"""Replays an iteration: the same start and the same decisions of the user, the branch implemented again.

    beck replay <iteration> [--into DIR] [--agents live|recorded|mixed] [--model AGENT=MODEL ...]

A replay starts a new iteration in an empty folder (or a clone at the
recorded start, for a brownfield project) with the recorded configuration
(the ConfigLoaded event), and runs beck's machine on it. Every decision of
the user gets the recorded answer. What answers the agents' turns is the
replay's mode:

* `live` (the default): the real agents implement the branch again, with
  today's prompts and beck, and the models of `--model` (a benchmark of a
  model, of a prompt, of a version of beck). The tests are theirs, run anew.
* `recorded`: each turn as it happened, from the events alone: the agent's
  JSON (`AgentTurnEnded.output`), its changes (`TurnChanged`, a git patch,
  applied again) and the `beck` commands it ran (`ToolCalled`, run again,
  so their test runs are evidence of this replay). beck's own steps run with
  the current code: an integration test of beck on real data. A turn the
  new code asks for and the recording lacks stops the replay.
* `mixed`: recorded, and the turns the recording lacks go to the real agents.

The decisions are matched by key (the interview) or question (an agent's),
the others in order; a decision the recording lacks stops the replay: answer
it with `beck answer`, then `--resume <replay>` goes on. The report compares
the two runs: their events, and each agent's turns, tokens and time.

Only the iterations recorded by the event-sourced core replay (its events hold the requests, the outputs,
the changes and the runs); the older ones are implemented again from scratch.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import yaml

from beck import socket
from beck.app import engine_for, finished, pending_decisions
from beck.events.project import Project
from beck.events.store import Event, EventStore
from beck.machine.core import Agents, Result, Turn


class Diverged(BaseException):
    """The new code asks for what the recording does not hold; a BaseException, so the engine lets it through."""


@dataclass
class Recorded:
    request: Event                          # the original AgentTurnRequested
    variant: tuple[str, int]                # ("first", 0), ("retry", n), ("answer", 0)
    output: Any = None
    tokens: dict[str, int] = field(default_factory=dict)
    model: str = "default"
    patch: str = ""                         # the turn's changes (TurnChanged)
    runs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)   # its beck send test/check, in order
    used: bool = False


def _variant(args: dict[str, Any]) -> tuple[str, int]:
    if args.get("retry"):
        return ("retry", int(args["retry"][0]))
    if "answer" in args:
        return ("answer", 0)
    return ("first", 0)


def _sent(run: Event) -> tuple[str, dict[str, Any]]:
    """What an agent sent the socket, from the run it made: the command again."""
    p = run.payload
    if run.type == "TestsRun":
        return "test", {"tests": p["tests"], "component": p.get("component"), "requirement": p.get("requirement")}
    return "check", {"paths": p["paths"], "component": p.get("component"), "all": p["whole_project"]}


class Recording:
    """The original iteration's answers, from its events: the agents' turns and the user's decisions."""

    def __init__(self, store: EventStore, root: Path, turns: bool = True):
        self.root = root
        events = list(store.read())
        caused: dict[str | None, list[Event]] = defaultdict(list)
        for e in events:
            caused[e.causation_id].append(e)
        self.config = next((e.payload["config"] for e in events if e.type == "ConfigLoaded"), None)
        self.turns: dict[tuple[str, str, str], list[Recorded]] = defaultdict(list)
        self.decisions: dict[str, list[tuple[Event, list[Result]]]] = defaultdict(list)
        self.failed = 0
        for e in events:
            if e.type == "DecisionRequested":
                answer = [r for r in caused[e.id] if r.actor == "user"]
                if answer:
                    self.decisions[e.payload["command"]].append(
                        (e, [Result(r.type, r.payload, actor=r.actor) for r in answer]))
                continue
            if e.type != "AgentTurnRequested" or not turns:
                continue
            effects = caused[e.id]
            ended = next((r for r in reversed(effects) if r.type == "AgentTurnEnded"), None)
            if ended is None:                # the CLI failed: nothing to replay
                self.failed += 1
                continue
            patch = next((r.payload["patch"] for r in reversed(effects) if r.type == "TurnChanged"), "")
            runs = [_sent(r) for r in effects if r.type in ("TestsRun", "ChecksRun") and not r.payload.get("hunt")]
            args = e.payload["args"]
            self.turns[(e.payload["target"], e.payload["command"], args["session"])].append(
                Recorded(e, _variant(args), ended.payload["output"], ended.payload.get("tokens", {}),
                         ended.payload.get("model", "default"), patch, runs))

    def take(self, agent: str, name: str, session: str, variant: tuple[str, int]) -> Recorded | None:
        """The next recorded turn of the session with this variant, skipping those the new code did not ask for."""
        queue = self.turns.get((agent, name, session), [])
        last = max((i for i, r in enumerate(queue) if r.used), default=-1)
        for r in queue[last + 1:]:
            if r.variant == variant:
                r.used = True
                return r
        return None

    def decide(self, request: Event) -> list[Result] | None:
        """The recorded answer to a decision of the user: by key or question when it has one, else in order."""
        name, args = request.payload["command"], request.payload["args"]
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
        for e in list(store.read(types_=("AgentTurnRequested", "DecisionRequested"))):
            effects = store.caused_by(e.id)
            if e.type == "DecisionRequested":
                if any(r.actor == "user" for r in effects):
                    self.decide(e)
                continue
            ended = next((r for r in effects if r.type == "AgentTurnEnded"), None)
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


@dataclass
class ReplayAgents:
    """The Agents of a replay: each turn's recorded output, its changes applied again in the new folder and its
    `beck send` commands sent again, so that its runs are the evidence of this replay."""
    recording: Recording
    root: Path                          # the replay's root
    iteration: str
    live: Agents | None = None          # mixed: the turns the recording lacks go to the real agents
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
        if r.patch:
            await asyncio.to_thread(self._patch, r.patch, cwd)
        for what, sent in r.runs:
            answer = socket.send(self.root, {"command": what, "cause": env["BECK_CAUSE"], "actor": agent,
                                             "args": {**sent, "cwd": str(cwd)}})
            if not answer.get("ok"):
                self.drifted.append(f"{agent} {envelope['command']}: beck send {what}: {answer.get('error')}")
        self.turns += 1
        old, new = str(self.recording.root), str(self.root)
        # no CLI session: none exists for a replayed turn, and a live turn of the session must start one
        return Turn(_moved(r.output, old, new), r.model, r.tokens, None, [])

    def _patch(self, patch: str, cwd: Path) -> None:
        done = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=cwd, input=patch, text=True,
                              capture_output=True)
        if done.returncode != 0:
            self.drifted.append(f"{cwd}: the patch does not apply: {done.stderr.strip()[:200]}")


@dataclass
class Outcome:
    root: Path
    iteration: str
    end: str                            # IterationClosed | crashed | diverged | stuck
    detail: str = ""
    seconds: int = 0


MODES = ("live", "recorded", "mixed")


def replay(original: Project, iteration: str, into: Path, out: TextIO, mode: str = "live",
           models: dict[str, str] | None = None, resume: str | None = None) -> Outcome:
    it = original.find(iteration)
    source = original.open(it.id)
    recording = Recording(source, original.root, turns=mode != "live")
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
        project, new, store = _start(original, source, recording, into, out, it.id, mode, models or {})
    live = _live(store, project, new.id) if mode != "recorded" else None
    agents: Agents = live if mode == "live" and live is not None else ReplayAgents(recording, project.root, new.id, live)
    engine = engine_for(project, new.id, store, agents)
    outcome = Outcome(project.root, new.id, "stuck")
    started = time.monotonic()
    try:
        outcome = asyncio.run(_drive(engine, recording, store, outcome))
    except Diverged as error:
        outcome.end, outcome.detail = "diverged", str(error)
    outcome.seconds = int(time.monotonic() - started)
    report(source, store, recording, agents if isinstance(agents, ReplayAgents) else None, outcome, out)
    store.close()
    source.close()
    return outcome


def _start(original: Project, source: EventStore, recording: Recording, into: Path, out: TextIO,
           iteration: str, mode: str, models: dict[str, str]) -> tuple[Project, Any, EventStore]:
    started = next(iter(source.read(types_=("SessionStarted",))))
    into.mkdir(parents=True, exist_ok=True)
    if started.sha:                     # a brownfield start: the repository as it was
        subprocess.run(["git", "clone", "-q", str(original.root), str(into)], check=True)
        subprocess.run(["git", "checkout", "-q", started.sha], cwd=into, check=True)
    config = dict(recording.config or {})   # the configuration as the original loaded it, the models overridden
    if models:
        config["models"] = {**config.get("models", {}), **models}
    if config:
        (into / ".beck").mkdir(exist_ok=True)
        (into / ".beck" / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    project = Project(into)
    new, store, _ = project.start_iteration()
    turns = sum(map(len, recording.turns.values()))
    out.write(f"beck: replaying {iteration[:8]} as {new.id[:8]} in {into} ({mode}"
              + (f", {', '.join(f'{a}={m}' for a, m in models.items())}" if models else "") + ")"
              + (f"; {turns} turns recorded, {recording.failed} failed turns skipped" if mode != "live" else "") + "\n")
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


async def _drive(engine: Any, recording: Recording, store: EventStore, outcome: Outcome) -> Outcome:
    while True:
        await engine.run_until_idle()
        if engine.crashes:
            c = engine.crashes[0].payload
            outcome.end, outcome.detail = "crashed", f"{c['where']} {c['name']}: {c['error']}"
            return outcome
        end = finished(store)
        if end is not None:
            outcome.end = end.type
            return outcome
        waiting = pending_decisions(engine)
        if not waiting:
            outcome.detail = "nothing to do and nothing to answer"
            return outcome
        for command in waiting:
            results = recording.decide(command)
            if results is None:
                raise Diverged(f"no recorded answer to the user's {command.payload['command']}: "
                               f"{json.dumps(command.payload['args'])[:300]}")
            engine.answer(command.id, results)


COMPARED = ("AgentTurnRequested", "SignalRejected", "BeckCrashed", "AgentTurnFailed", "RedRewound", "CycleReopened",
            "CycleDropped", "ComponentParked", "RefactorRejected", "RestartProposed", "RegressionFound",
            "GreenCommitted", "ComponentLanded")


def _counts(store: EventStore) -> Counter[str]:
    c: Counter[str] = Counter()
    for e in store.read():
        if e.type in COMPARED:
            c[e.type] += 1
    return c


def _usage(store: EventStore) -> dict[str, list[int]]:
    """Per agent: turns, tokens (input + output), seconds of turns."""
    usage: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for e in store.read(types_=("AgentTurnEnded",)):
        u = usage[e.payload["agent"]]
        u[0] += 1
        u[1] += e.payload["tokens"].get("input", 0) + e.payload["tokens"].get("output", 0)
        u[2] += e.payload.get("duration_ms", 0) // 1000
    return usage


def report(source: EventStore, store: EventStore, recording: Recording, agents: ReplayAgents | None,
           outcome: Outcome, out: TextIO) -> None:
    out.write(f"beck: replay {outcome.end}" + (f": {outcome.detail}" if outcome.detail else "")
              + f" ({outcome.seconds} s)\n")
    before, after = _counts(source), _counts(store)
    out.write(f"{'':32}{'recorded':>10}{'replay':>10}\n")
    for k in sorted(set(before) | set(after), key=lambda k: (k.startswith(" "), k)):
        out.write(f"{k:32}{before[k]:>10}{after[k]:>10}\n")
    ub, ua = _usage(source), _usage(store)
    out.write(f"{'agent: turns, tokens, s':32}{'recorded':>22}{'replay':>22}\n")
    for a in sorted(set(ub) | set(ua)):
        out.write(f"{a:32}{' / '.join(map(str, ub.get(a, [0, 0, 0]))):>22}{' / '.join(map(str, ua.get(a, [0, 0, 0]))):>22}\n")
    if agents is not None:
        out.write(f"turns replayed: {agents.turns}; live: {len(agents.lived)}; drifted: {len(agents.drifted)}\n")
        for t in agents.lived:
            out.write(f"  live {t}\n")
        for d in agents.drifted[:10]:
            out.write(f"  drifted {d}\n")
        for k, n in recording.unused().most_common():
            out.write(f"  not asked again: {k} ×{n}\n")
    out.write(f"the replay's events: beck events {outcome.iteration[:8]} --project {outcome.root}\n")


def run(project_root: Path, iteration: str, into: Path | None, out: TextIO | None = None, mode: str = "live",
        models: dict[str, str] | None = None, resume: str | None = None) -> int:
    stream = out or sys.stdout
    if resume and into is None:
        stream.write("beck: --resume needs the replay's folder (--into)\n")
        return 1
    target = into or Path(tempfile.mkdtemp(prefix="beck-replay-"))
    if not resume and target.exists() and any(target.iterdir()):
        stream.write(f"beck: {target} is not empty\n")
        return 1
    outcome = replay(Project(project_root), iteration, target, stream, mode=mode, models=models, resume=resume)
    return 0 if outcome.end == "IterationClosed" else 1
