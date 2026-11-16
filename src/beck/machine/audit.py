"""smart_audit: the deterministic observer for argyris (README, "Actors").

After every agent's turn it reads the turn's tool calls from the event store
and looks for the violations a rule can decide; each goes to the outbox of
.beck/beck.db (with the event it is about and its correlation) and is
recorded as a ViolationLogged event. The component that wakes argyris reads
the outbox (`beck audit`). When a violation reaches argyris without an
audit, a rule is missing here; when a rule needs judgement, a small agent
comes back inside this component as its fallback.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from beck.drivers.claude import allowed_tools
from beck.events.project import Project
from beck.events.store import Event
from beck.index.anchor import is_test_file
from beck.machine.core import Command, Ctx, Machine, Result
from beck.machine.unit import plan

audit = Machine()
A = "audit"
WRITES = ("Edit", "Write", "MultiEdit", "NotebookEdit")


@audit.policy("AgentTurnEnded", "SignalRejected", "AgentTurnFailed", "BeckCrashed")
def audit_the_turn(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("AuditTurn", "beck", {"event": event.id}, A)]


_OPERATORS = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def parts(command: str) -> list[str]:
    """The simple commands of a shell line, as the CLI checks them: split on && || ; | and newlines, a
    heredoc's body left out (it is data, not commands)."""
    out: list[str] = []
    ending: str | None = None
    for line in command.splitlines():
        if ending is not None:
            if line.strip() == ending:
                ending = None
            continue
        heredoc = _HEREDOC.search(line)
        if heredoc:
            ending = heredoc.group(1)
        out += [p for p in _OPERATORS.split(line) if p.strip()]
    return out


def _allowed(agent: str, tool: str, tool_input: dict[str, Any]) -> bool:
    if tool == "Bash" and len(parts(str(tool_input.get("command", "")))) > 1:
        return all(_allowed(agent, tool, {**tool_input, "command": p}) for p in parts(str(tool_input["command"])))
    for rule in allowed_tools(agent):
        name, _, pattern = rule.partition("(")
        if name != tool:
            continue
        if not pattern:
            return True
        command = str(tool_input.get("command", ""))
        prefix = pattern.rstrip(")").removesuffix(":*").removesuffix("*")
        if command == prefix or command.startswith(prefix + " ") or fnmatch.fnmatch(command, pattern.rstrip(")")):
            return True
    return False


READERS = ("cat", "bat", "head", "tail", "less")
SHELL_OPERATORS = {"|", "||", "&&", ";", "&"}
PATHLIKE = re.compile(r"[\w./@+-]+")


def _read_paths(root: Path, tool: dict[str, Any]) -> list[str]:
    """The files a tool call reads: a Read's, or the file arguments of a shell reader (cat, bat, head...)."""
    if tool["tool"] == "Read":
        p = _path(root, tool["input"])
        return [p] if p else []
    if tool["tool"] != "Bash":
        return []
    found: list[str] = []
    for part in parts(str(tool["input"].get("command", ""))):
        found += _shell_reads(root, part)
    return found


def _shell_reads(root: Path, command: str) -> list[str]:
    words = command.split()
    if not words or words[0] not in READERS:
        return []
    files = []
    for w in words[1:]:
        if w in SHELL_OPERATORS or w.startswith(("<", ">", "|")):   # a heredoc, a redirection, a pipe: no more reads
            break
        if PATHLIKE.fullmatch(w) and ("/" in w or "." in w) and not w.startswith("-"):
            files.append(w)
    return [p for p in (_path(root, {"file_path": f}) for f in files) if p]


def _path(root: Path, tool_input: dict[str, Any]) -> str | None:
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not raw:
        return None
    p = Path(str(raw))
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p)
    return str(p)


def violations(ctx: Ctx, event: Event) -> list[tuple[str, dict[str, Any]]]:
    """The violations of one turn, as (kind, detail)."""
    if event.type == "SignalRejected":
        return [("malformed-output", {"command": event.payload.get("command"), "errors": event.payload["errors"][:5]})]
    if event.type == "BeckCrashed":   # beck's own bug: argyris reads the traceback
        return [("beck-crashed", {"agent": "beck", **{k: event.payload[k] for k in ("where", "name", "error",
                                                                                   "traceback")}})]
    if event.type == "AgentTurnFailed":
        return [("turn-failed", {"command": event.payload["command"], "error": event.payload["error"][:500]})]
    turn = ctx.store.get(event.causation_id or "")          # the agent's CommandIssued
    agent, command, args = turn.payload["target"], turn.payload["command"], turn.payload["args"]
    siblings = ctx.store.caused_by(turn.id)
    tools = [e.payload for e in siblings if e.type == "ToolCalled"]
    found: list[tuple[str, dict[str, Any]]] = []
    for t in tools:
        if not _allowed(agent, t["tool"], t["input"]):
            kind = "forbidden-command" if t["tool"] == "Bash" else "forbidden-tool"
            found.append((kind, {"command": command, "tool": t["tool"], "input": t["input"]}))
    unit = args.get("unit")
    root = ctx.workspace.worktree(unit) if unit and agent in ("unclebob", "martin") else ctx.project
    if agent == "unclebob" and unit:   # its own scope, and the code of the units it depends on
        scope = ctx.workspace.scope(unit) + [p for d in plan(ctx).get(unit, {}).get("depends_on", [])
                                             for p in ctx.workspace.scope(d)]
        for t in tools:
            for p in _read_paths(root, t):
                if not is_test_file(Path(p)) and not any(p == s or p.startswith(s.rstrip("/") + "/") for s in scope):
                    found.append(("explored", {"command": command, "path": p}))
        runs = [e for e in siblings if e.type == "TestsRun"]
        test = args.get("test")
        for r in runs:
            if test and r.payload["tests"] != [test]:
                found.append(("tests-outside-the-cycle", {"command": command, "tests": r.payload["tests"]}))
    if agent == "martin" and command == "RefactorUnit" and "weak_tests" in args.get("context", {}):
        writes = [w for w in (_path(root, t["input"]) for t in tools if t["tool"] in WRITES) if w]
        first_test = next((i for i, w in enumerate(writes) if is_test_file(Path(w))), None)
        first_code = next((i for i, w in enumerate(writes) if not is_test_file(Path(w))), None)
        if first_code is not None and (first_test is None or first_code < first_test):
            found.append(("tdd-order", {"command": command, "writes": writes[:10]}))
    return found


@audit.handler("AuditTurn")
def handle_audit(command: Event, ctx: Ctx) -> list[Result]:
    event = ctx.store.get(command.payload["args"]["event"])
    found = violations(ctx, event)
    if not found:
        return []
    project = Project(ctx.project)
    results = []
    try:
        for kind, detail in found:
            agent = detail.get("agent") or event.payload.get("agent", "?")
            record = project.post(ctx.iteration, event, kind, {"agent": agent, **detail})
            results.append(Result("ViolationLogged", {"kind": kind, "agent": agent, "outbox": record.id}))
    finally:
        project.close()
    return results
