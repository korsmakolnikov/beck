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
from pathlib import Path
from typing import Any

from beck.drivers.claude import allowed_tools
from beck.events.project import Project
from beck.events.store import Event
from beck.index.anchor import is_test_file
from beck.machine.core import Command, Ctx, Machine, Result

audit = Machine()
A = "audit"
WRITES = ("Edit", "Write", "MultiEdit", "NotebookEdit")


@audit.policy("AgentTurnEnded", "SignalRejected", "AgentTurnFailed")
def audit_the_turn(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("AuditTurn", "beck", {"event": event.id}, A)]


def _allowed(agent: str, tool: str, tool_input: dict[str, Any]) -> bool:
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
    if agent == "unclebob" and unit:
        scope = ctx.workspace.scope(unit)
        for t in tools:
            p = _path(root, t["input"]) if t["tool"] in ("Read",) else None
            if p and not is_test_file(Path(p)) and not any(p == s or p.startswith(s.rstrip("/") + "/") for s in scope):
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
            agent = event.payload.get("agent", "?")
            record = project.post(ctx.iteration, event, kind, {"agent": agent, **detail})
            results.append(Result("ViolationLogged", {"kind": kind, "agent": agent, "outbox": record.id}))
    finally:
        project.close()
    return results
