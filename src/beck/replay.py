"""Replays an iteration with the current code: the agents' and the user's answers as recorded, beck's own steps anew.

    beck replay <iteration> [--into DIR]

A replay starts a new iteration in an empty folder (or a clone at the
recorded start, for a brownfield project) and runs beck's machine on it.
Every agent turn the machine issues is answered from the recording: its
JSON answer and its changes to the files (Write, Edit, `git rm`, the
`beck` commands it ran), taken from the agent's Claude Code transcript and
re-applied with the paths moved to the new folder. Every decision of the
user gets the recorded answer. beck's own steps (verifications, commits,
tests, landings, audits) run with the current code, so the replay is an
integration test of beck on real data: it says whether the new code
crashes, where it takes another way, and how its events compare.

* An answer is matched by agent, command and session; a retry by its
  attempt. A recorded turn the new code does not ask for is skipped (a
  rejection fixed since, a failure of the CLI); a turn it asks for and the
  recording lacks stops the replay: there the new code has taken another way.
  With `--live` that turn goes to the real agent instead, and the replay
  goes on. A decision of the user the recording lacks always stops it: the
  replay is an iteration like any other: answer it with `beck answer`, then
  `beck replay <iteration> --into DIR --resume <replay>` goes on with the
  recording (or `beck tui <replay>`, with the real agents only).
* A failed turn (the account's limit, a crash of the CLI) is never replayed.
* The DSL files the agents write are migrated (`beck.dsl.validate.migrate`):
  a recording made before a key was renamed still replays.
* An edit whose text is no longer in the file is counted, not applied: the
  files have drifted from the recording.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from beck.app import finished, pending_decisions, reactor_for
from beck.drivers.claude import INPUT_CHARS, parse_answer, session_id
from beck.dsl.validate import migrate
from beck.events.project import Project
from beck.events.store import Event, EventStore
from beck.machine.audit import parts
from beck.machine.core import Agents, Result, Turn

FILE_TOOLS = {"Write", "Edit", "MultiEdit"}
REPLAYED_SHELL = ("beck ", "git rm ")   # an agent's commands with effects: the rest only read


class Diverged(BaseException):
    """The new code asks for what the recording does not hold; a BaseException, so the reactor lets it through."""


@dataclass
class Recorded:
    command: Event
    variant: tuple[str, int]                # ("first", 0), ("retry", n), ("answer", 0)
    output: Any = None
    tools: list[dict[str, Any]] = field(default_factory=list)   # {"tool", "input", "ok"}
    tokens: dict[str, int] = field(default_factory=dict)
    model: str = "default"
    used: bool = False


def _variant(args: dict[str, Any]) -> tuple[str, int]:
    if args.get("retry"):
        return ("retry", int(args["retry"][0]))
    if "answer" in args:
        return ("answer", 0)
    return ("first", 0)


def transcripts_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def _transcript(sid: str, root: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in sorted(root.glob(f"*/{sid}.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return lines


def _content(m: dict[str, Any]) -> Any:
    message = m.get("message")
    return message.get("content") if isinstance(message, dict) else None


def read_turn(transcript: list[dict[str, Any]], command_id: str) -> tuple[Any, list[dict[str, Any]]] | None:
    """The answer and the tool calls of the turn whose envelope names command_id; None when it is not there.

    A turn runs from its envelope (a user message as text) to the next one; its answer is its last text.
    """
    messages = [m for m in transcript if m.get("type") in ("user", "assistant") and not m.get("isSidechain")]
    starts = [i for i, m in enumerate(messages) if m["type"] == "user" and isinstance(_content(m), str)
              and f'"event_id": "{command_id}"' in _content(m)]
    if not starts:
        return None
    i = starts[-1] + 1                       # the last: a turn resumed after "already in use" sent it twice
    uses: list[dict[str, Any]] = []
    errors: set[str] = set()
    answer: str | None = None
    while i < len(messages) and not (messages[i]["type"] == "user" and isinstance(_content(messages[i]), str)):
        for block in _content(messages[i]) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append({"id": block.get("id"), "tool": block.get("name", "?"), "input": block.get("input", {})})
            elif block.get("type") == "tool_result" and block.get("is_error"):
                errors.add(str(block.get("tool_use_id")))
            elif block.get("type") == "text" and messages[i]["type"] == "assistant":
                answer = block.get("text", "")
        i += 1
    if answer is None:
        return None
    return parse_answer(answer), [{"tool": u["tool"], "input": u["input"], "ok": u["id"] not in errors} for u in uses]


class Recording:
    """The original iteration's answers: the agents' turns, from their transcripts, and the user's decisions."""

    def __init__(self, store: EventStore, iteration: str, root: Path, transcripts: Path | None = None):
        self.root = root
        events = list(store.read())
        caused: dict[str | None, list[Event]] = defaultdict(list)
        for e in events:
            caused[e.causation_id].append(e)
        self.turns: dict[tuple[str, str, str], list[Recorded]] = defaultdict(list)
        self.decisions: dict[str, list[tuple[Event, list[Result]]]] = defaultdict(list)
        self.missing: list[Event] = []      # turns that ended but whose transcript was not found
        self.failed = 0
        cache: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            if e.type != "CommandIssued" or e.payload["target"] == "beck":
                continue
            target, name, args = e.payload["target"], e.payload["command"], e.payload["args"]
            results = [r for r in caused[e.id] if r.type != "CommandDone"]
            if target == "user":
                if results:
                    self.decisions[name].append((e, [Result(r.type, r.payload, actor=r.actor) for r in results]))
                continue
            ended = next((r for r in results if r.type == "AgentTurnEnded"), None)
            if ended is None:                # the CLI failed: nothing to replay
                self.failed += 1
                continue
            sid = session_id(iteration, target, args["session"])
            if sid not in cache:
                cache[sid] = _transcript(sid, transcripts or transcripts_root())
            turn = read_turn(cache[sid], e.id)
            if turn is None:
                self.missing.append(e)
                continue
            output, tools = turn
            self.turns[(target, name, args["session"])].append(
                Recorded(e, _variant(args), output, tools, ended.payload.get("tokens", {}),
                         ended.payload.get("model", "default")))

    def take(self, agent: str, name: str, session: str, variant: tuple[str, int]) -> Recorded | None:
        """The next recorded turn of the session with this variant, skipping those the new code did not ask for."""
        queue = self.turns.get((agent, name, session), [])
        last = max((i for i, r in enumerate(queue) if r.used), default=-1)
        for r in queue[last + 1:]:
            if r.variant == variant:
                r.used = True
                return r
        return None

    def decide(self, command: Event) -> list[Result] | None:
        """The recorded answer to a decision of the user: by key or question when it has one, else in order."""
        name, args = command.payload["command"], command.payload["args"]
        queue = self.decisions.get(name, [])

        def same(original: Event) -> bool:
            o = original.payload["args"]
            if name == "AskInterview":
                return bool(o["key"] == args["key"])
            if name == "Ask":
                return bool(o["question"]["text"] == args["question"]["text"])
            return True
        for i, (original, results) in enumerate(queue):
            if same(original):
                del queue[i]
                return results
        return None

    def consume(self, store: EventStore) -> None:
        """Marks used what a replay already took, to resume it: its replayed turns (no CLI session) and decisions."""
        events = list(store.read())
        caused: dict[str | None, list[Event]] = defaultdict(list)
        for e in events:
            caused[e.causation_id].append(e)
        for e in events:
            if e.type != "CommandIssued" or e.payload["target"] == "beck":
                continue
            results = caused[e.id]
            if e.payload["target"] == "user":
                if any(r.type == "CommandDone" for r in results):
                    self.decide(e)
                continue
            ended = next((r for r in results if r.type == "AgentTurnEnded"), None)
            if ended is not None and "session" not in ended.payload:
                self.take(e.payload["target"], e.payload["command"], e.payload["args"]["session"],
                          _variant(e.payload["args"]))

    def unused(self) -> Counter[str]:
        return Counter(f"{k[0]} {k[1]}" for k, rs in self.turns.items() for r in rs if not r.used)


def _moved(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_moved(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _moved(v, old, new) for k, v in value.items()}
    return value


def _is_dsl(path: Path) -> bool:
    return path.parent.name == ".beck" and path.name in ("domain.yaml", "architecture.yaml")


@dataclass
class ReplayAgents:
    """The Agents of a replay: each turn's recorded answer, its file changes applied again in the new folder."""
    recording: Recording
    root: Path                          # the replay's root
    iteration: str
    live: Agents | None = None          # --live: the turns the recording lacks go to the real agents
    drifted: list[str] = field(default_factory=list)
    turns: int = 0
    lived: list[str] = field(default_factory=list)

    async def turn(self, agent: str, session: str, envelope: dict[str, Any], env: dict[str, str],
                   cwd: Path) -> Turn:
        args = {"retry": [envelope["retry"]["attempt"]]} if envelope.get("retry") else {}
        if "answer" in envelope:
            args["answer"] = envelope["answer"]
        r = self.recording.take(agent, envelope["command"], session, _variant(args))
        if r is None and self.live is not None:
            self.lived.append(f"{agent} {envelope['command']} {session}")
            return await self.live.turn(agent, session, envelope, env, cwd)
        if r is None:
            raise Diverged(f"no recorded {agent} {envelope['command']} for session {session} "
                           f"({'/'.join(map(str, _variant(args)))})")
        old, new = str(self.recording.root), str(self.root)
        tools = []
        for t in r.tools:
            moved = _moved(t["input"], old, new)
            if t["ok"]:
                await asyncio.to_thread(self._apply, t["tool"], moved, cwd, env)
            text = json.dumps(moved)
            tools.append({"tool": t["tool"], "input": moved if len(text) <= INPUT_CHARS else {"truncated": text[:INPUT_CHARS]}})
        self.turns += 1
        # no CLI session: none exists for a replayed turn, and a live turn of the session must start one
        return Turn(_moved(r.output, old, new), r.model, r.tokens, None, tools)

    def _apply(self, tool: str, input_: dict[str, Any], cwd: Path, env: dict[str, str]) -> None:
        if tool in FILE_TOOLS and _is_dsl(Path(input_["file_path"])):   # recorded before a change of the DSLs
            input_ = {k: migrate(v) if k in ("content", "old_string", "new_string") else v for k, v in input_.items()}
            if "edits" in input_:
                input_["edits"] = [{k: migrate(v) if isinstance(v, str) else v for k, v in e.items()}
                                   for e in input_["edits"]]
        if tool == "Write":
            path = Path(input_["file_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(input_["content"])
        elif tool in ("Edit", "MultiEdit"):
            path = Path(input_["file_path"])
            edits = input_.get("edits") or [input_]
            text = path.read_text() if path.is_file() else ""
            for e in edits:
                if e["old_string"] not in text:
                    self.drifted.append(f"{path}: {e['old_string'][:60]!r}")
                    return
                text = (text.replace(e["old_string"], e["new_string"]) if e.get("replace_all")
                        else text.replace(e["old_string"], e["new_string"], 1))
            path.write_text(text)
        elif tool == "Bash":
            where = cwd
            for part in parts(str(input_.get("command", ""))):
                part = part.strip()
                if part.startswith("cd "):
                    where = where / Path(shlex.split(part)[1])
                elif part.startswith(REPLAYED_SHELL):
                    subprocess.run(part, shell=True, cwd=where, env={**os.environ, **env}, capture_output=True,
                                   timeout=900)


@dataclass
class Outcome:
    root: Path
    iteration: str
    end: str                            # IterationClosed | crashed | diverged | stuck
    detail: str = ""


def replay(original: Project, iteration: str, into: Path, out: TextIO, transcripts: Path | None = None,
           live: bool = False, resume: str | None = None) -> Outcome:
    it = original.find(iteration)
    source = original.open(it.id)
    recording = Recording(source, it.id, original.root, transcripts)
    if resume:
        project = Project(into)
        new = project.find(resume)
        store = project.open(new.id)
        recording.consume(store)
        last = store.last()
        store.append("SessionAttached", {"from_seq": last.seq if last else 0},
                     cause=next(iter(store.read(types_=("SessionStarted",))), None))
        out.write(f"beck: resuming the replay {new.id[:8]} of {it.id[:8]} in {into}\n")
    else:
        project, new, store = _start(original, source, recording, into, out, it.id)
    agents = ReplayAgents(recording, project.root, new.id, _live(store, project, new.id) if live else None)
    reactor = reactor_for(project, new.id, store, agents)
    outcome = Outcome(project.root, new.id, "stuck")
    try:
        outcome = asyncio.run(_drive(reactor, recording, store, outcome))
    except Diverged as error:
        outcome.end, outcome.detail = "diverged", str(error)
    report(source, store, recording, agents, outcome, out)
    store.close()
    source.close()
    return outcome


def _start(original: Project, source: EventStore, recording: Recording, into: Path, out: TextIO,
           iteration: str) -> tuple[Project, Any, EventStore]:
    started = next(iter(source.read(types_=("SessionStarted",))))
    into.mkdir(parents=True, exist_ok=True)
    if started.sha:                     # a brownfield start: the repository as it was
        subprocess.run(["git", "clone", "-q", str(original.root), str(into)], check=True)
        subprocess.run(["git", "checkout", "-q", started.sha], cwd=into, check=True)
    for name in ("config.yml",):
        if (original.beck_dir / name).is_file():
            (into / ".beck").mkdir(exist_ok=True)
            (into / ".beck" / name).write_bytes((original.beck_dir / name).read_bytes())
    project = Project(into)
    new, store, _ = project.start_iteration()
    out.write(f"beck: replaying {iteration[:8]} as {new.id[:8]} in {into}; "
              f"{sum(map(len, recording.turns.values()))} turns recorded, {recording.failed} failed turns skipped, "
              f"{len(recording.missing)} without a transcript\n")
    return project, new, store


def _live(store: EventStore, project: Project, iteration: str) -> Agents:
    from beck import config as beck_config
    from beck.app import docs_dir
    from beck.drivers.claude import ClaudeCode
    try:
        models = beck_config.load(project.root)["models"]
    except beck_config.ConfigError:
        models = beck_config.DEFAULTS["models"]
    return ClaudeCode(store, project.root, iteration, models, executable=os.environ.get("BECK_CLAUDE", "claude"),
                      docs=docs_dir())


async def _drive(reactor: Any, recording: Recording, store: EventStore, outcome: Outcome) -> Outcome:
    while True:
        await reactor.run_until_idle()
        if reactor.crashes:
            c = reactor.crashes[0].payload
            outcome.end, outcome.detail = "crashed", f"{c['where']} {c['name']}: {c['error']}"
            return outcome
        end = finished(store)
        if end is not None:
            outcome.end = end.type
            return outcome
        waiting = pending_decisions(reactor)
        if not waiting:
            outcome.detail = "nothing to do and nothing to answer"
            return outcome
        for command in waiting:
            results = recording.decide(command)
            if results is None:
                raise Diverged(f"no recorded answer to the user's {command.payload['command']}: "
                               f"{json.dumps(command.payload['args'])[:300]}")
            reactor.answer(command.id, results)


COMPARED = ("CommandIssued", "SignalRejected", "BeckCrashed", "AgentTurnFailed", "RedRewound", "CycleReopened",
            "CycleDropped", "UnitParked", "RefactorRejected", "ResolutionProposed", "RegressionFound",
            "GreenCommitted", "UnitLanded", "ViolationLogged")


def _counts(store: EventStore) -> Counter[str]:
    c: Counter[str] = Counter()
    for e in store.read():
        if e.type in COMPARED and e.type != "CommandIssued":
            c[e.type] += 1
            if e.type == "ViolationLogged":
                c[f"  {e.payload.get('kind')}"] += 1
    return c


def report(source: EventStore, store: EventStore, recording: Recording, agents: ReplayAgents, outcome: Outcome,
           out: TextIO) -> None:
    out.write(f"beck: replay {outcome.end}" + (f": {outcome.detail}" if outcome.detail else "") + "\n")
    before, after = _counts(source), _counts(store)
    out.write(f"{'':32}{'recorded':>10}{'replay':>10}\n")
    for k in sorted(set(before) | set(after), key=lambda k: (k.startswith(" "), k)):
        out.write(f"{k:32}{before[k]:>10}{after[k]:>10}\n")
    out.write(f"turns replayed: {agents.turns}; live: {len(agents.lived)}; edits drifted: {len(agents.drifted)}\n")
    for t in agents.lived:
        out.write(f"  live {t}\n")
    for d in agents.drifted[:10]:
        out.write(f"  drifted {d}\n")
    for k, n in recording.unused().most_common():
        out.write(f"  not asked again: {k} ×{n}\n")
    out.write(f"the replay's events: beck events {outcome.iteration[:8]} --project {outcome.root}\n")


def run(project_root: Path, iteration: str, into: Path | None, out: TextIO | None = None, live: bool = False,
        resume: str | None = None) -> int:
    stream = out or sys.stdout
    if resume and into is None:
        stream.write("beck: --resume needs the replay's folder (--into)\n")
        return 1
    target = into or Path(tempfile.mkdtemp(prefix="beck-replay-"))
    if not resume and target.exists() and any(target.iterdir()):
        stream.write(f"beck: {target} is not empty\n")
        return 1
    outcome = replay(Project(project_root), iteration, target, stream, live=live, resume=resume)
    return 0 if outcome.end == "IterationClosed" else 1
