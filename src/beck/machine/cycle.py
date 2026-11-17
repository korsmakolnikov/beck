"""The machine of a cycle: one requirement, one test, one red and one green (README, phase 4).

    CycleOpened → WriteRedTest (unclebob) → RedDeclared → VerifyRed → RedVerified → CommitRed → RedCommitted
      → WriteGreenCode (unclebob) → GreenDeclared → VerifyGreen → GreenVerified → CommitGreen
      → GreenCommitted, CycleClosed

A rejection (RedRejected, GreenRejected, a SignalRejected of the turn) sends
the turn again with the reason, until the repetition rule blocks the cycle
(CycleBlocked). An agent's change request (ChangeRequested) ends the turn;
the component's machine parks the component. While a component is parked no turn of its
cycles starts; CycleReopened resumes a cycle at its phase.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beck.events.store import Event
from beck.index.store import INTEGRATION
from beck.machine import verify
from beck.machine.core import Command, Ctx, Issued, Machine, Result, request_of
from beck.machine.signatures import blocked, signature

cycle = Machine()

RED = ("RedRejected",)
GREEN = ("GreenRejected",)


def stream(component: str, requirement: str) -> str:
    return f"cycle:{component}/{requirement}"


@dataclass(frozen=True)
class Workspace:
    """Where a component lives and what it may touch, given by hand (the tests); beck.machine.component derives it."""
    worktrees: dict[str, Path]
    scopes: dict[str, list[str]]
    units: dict[tuple[str, str], dict[str, Any]]   # (component, unit) → beck index context
    met: set[tuple[str, str]] = field(default_factory=set)   # (component, requirement) the index shows implemented

    def worktree(self, component: str) -> Path:
        return self.worktrees[component]

    def already_met(self, component: str, requirement: str) -> bool:
        return (component, requirement) in self.met

    def scope(self, component: str) -> list[str]:
        return self.scopes[component]

    def unit_context(self, component: str, unit: str) -> dict[str, Any]:
        return self.units[(component, unit)]


def parked(ctx: Ctx, component: str) -> bool:
    """Whether the component is parked, as of the policy's event. A component parked before it started (a dependent, still
    waiting) is no longer parked once started."""
    state = False
    for e in ctx.events(stream=f"component:{component}", types=("ComponentParked", "ComponentResumed", "ComponentStarted")):
        state = e.type == "ComponentParked"
    return state


def _rejections(ctx: Ctx, component: str, requirement: str, kinds: tuple[str, ...], command: str, since: str) -> list[str]:
    """The signatures of the loop's rejections since its start event, in order."""
    events = ctx.events(stream=stream(component, requirement))
    start = max((e.seq for e in events if e.type == since), default=0)
    return [e.payload["signature"] for e in events if e.seq >= start and (
        e.type in kinds or (e.type == "SignalRejected" and e.payload.get("command") == command))]


def _red_turn(ctx: Ctx, component: str, requirement: str, unit: str, first: bool, retry: tuple[int, str] | None) -> Command:
    context: dict[str, Any] = {"requirement": requirement,
                               "run": f"beck send test <the test> --component {component} --requirement {requirement}"}
    if first and retry is None:
        context["unit"] = ctx.workspace.unit_context(component, unit)
    args: dict[str, Any] = {"session": f"{component}/{unit}", "context": context, "component": component,
                            "requirement": requirement,
                            "unit": unit}
    if retry:
        args["retry"] = list(retry)
    return Command("WriteRedTest", "unclebob", args, stream(component, requirement))


def _green_turn(component: str, requirement: str, unit: str, test: str, failure: str | None,
                retry: tuple[int, str] | None) -> Command:
    context: dict[str, Any] = {"requirement": requirement, "test": test,
                               "run": f"beck send test {test} --component {component} --requirement {requirement}"}
    if failure:
        context["failure"] = failure
    args: dict[str, Any] = {"session": f"{component}/{unit}", "context": context, "component": component,
                            "requirement": requirement,
                            "unit": unit, "test": test}
    if retry:
        args["retry"] = list(retry)
    return Command("WriteGreenCode", "unclebob", args, stream(component, requirement))


def _issued(ctx: Ctx, event: Event) -> Event:
    """The agent's turn (AgentTurnRequested) an event descends from."""
    return request_of(ctx.store, event)


# --- policies ----------------------------------------------------------------


@cycle.policy("CycleOpened")
def open_red(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if parked(ctx, p["component"]):
        return []
    return [_red_turn(ctx, p["component"], p["requirement"], p["unit"], p["first"], None)]


@cycle.policy("CycleReopened")
def reopen(event: Event, ctx: Ctx) -> list[Command]:
    """After a restart: the cycle goes on at its phase, the reason in the retry."""
    p = event.payload
    retry = (2, p["reason"])
    if p["phase"] == "red":
        return [_red_turn(ctx, p["component"], p["requirement"], p["unit"], False, retry)]
    test, unit = _declared_test(ctx, p["component"], p["requirement"])
    return [_green_turn(p["component"], p["requirement"], unit, test, None, retry)]


@cycle.policy("RedDeclared")
def verify_red(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [Command("VerifyRed", "beck", {**p, "turn": event.causation_id}, event.stream)]


@cycle.policy("RedRejected", "SignalRejected")
def retry_red(event: Event, ctx: Ctx) -> list[Command]:
    name, a = _agent_turn(ctx, event)
    if name != "WriteRedTest" or parked(ctx, a["component"]):
        return []
    return _retry(ctx, event, a["component"], a["requirement"], RED, name, "CycleOpened",
                  lambda attempt, reason: _red_turn(ctx, a["component"], a["requirement"], a["unit"], False, (attempt, reason)))


@cycle.policy("RedVerified")
def commit_red(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitRed", "beck", dict(event.payload), event.stream)]


@cycle.policy("RedCommitted")
def open_green(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    verified = ctx.store.get(event.causation_id or "")                 # CommitRed ran on the RedVerified
    if parked(ctx, p["component"]) or verified.payload.get("already_green"):
        return []
    test, unit = _declared_test(ctx, p["component"], p["requirement"])
    return [_green_turn(p["component"], p["requirement"], unit, test, None, None)]


@cycle.policy("GreenDeclared")
def verify_green(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifyGreen", "beck", {**event.payload, "turn": event.causation_id}, event.stream)]


@cycle.policy("GreenRejected", "SignalRejected")
def retry_green(event: Event, ctx: Ctx) -> list[Command]:
    name, a = _agent_turn(ctx, event)
    if name != "WriteGreenCode" or parked(ctx, a["component"]):
        return []
    failure = event.payload.get("failure")
    return _retry(ctx, event, a["component"], a["requirement"], GREEN, name, "RedCommitted",
                  lambda attempt, reason: _green_turn(a["component"], a["requirement"], a["unit"], a["test"], failure,
                                                      (attempt, reason)))


def green_stuck(ctx: Ctx, event: Event) -> bool:
    """A ChangeRequested that takes the cycle back to red: unclebob's `test` kind, or the repetition rule's
    times of change requests from the green of one red (no restart could unblock it: the red is suspect)."""
    p = event.payload
    if p["agent"] != "unclebob" or "requirement" not in p:
        return False
    if p["kind"] == "test":
        return True
    events = ctx.events(stream=stream(p["component"], p["requirement"]))
    since = max((e.seq for e in events if e.type in ("RedCommitted", "RedRewound")), default=0)
    asked = [e for e in events if e.seq > since and e.type == "ChangeRequested"]
    return _issued(ctx, event).payload["command"] == "WriteGreenCode" and len(asked) >= ctx.limits[0]


@cycle.policy("ChangeRequested")
def rewind_red(event: Event, ctx: Ctx) -> list[Command]:
    """unclebob, in a green turn, finds the committed red test wrong: back to red, its commit undone. A green
    may not change a test, so only a new red can fix it; the repetition rule bounds the rewinds."""
    p = event.payload
    if not green_stuck(ctx, event):
        return []
    a = _issued(ctx, event).payload["args"]
    rewinds = len(ctx.events(stream=stream(a["component"], a["requirement"]), types=("RedRewound",)))
    if rewinds >= ctx.limits[0]:
        return [Command("BlockCycle", "beck", {"component": a["component"], "requirement": a["requirement"],
                                               "reason": f"the red test was found wrong {rewinds} times"},
                        event.stream)]
    why = "the red test is wrong" if p["kind"] == "test" else "the green asked for a change too many times"
    return [Command("RewindRed", "beck", {"component": a["component"], "requirement": a["requirement"], "unit": a["unit"],
                                          "reason": f"{why}: {p['problem']}"}, event.stream)]


@cycle.policy("RedRewound")
def red_again(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if parked(ctx, p["component"]):
        return []
    attempt = len(ctx.events(stream=stream(p["component"], p["requirement"]), types=("RedRewound",))) + 1
    return [_red_turn(ctx, p["component"], p["requirement"], p["unit"], False, (attempt, p["reason"]))]


@cycle.policy("GreenVerified")
def commit_green(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitGreen", "beck", dict(event.payload), event.stream)]


def _retry(ctx: Ctx, event: Event, component: str, requirement: str, kinds: tuple[str, ...], command: str, since: str,
           turn: Any) -> list[Command]:
    sequence = _rejections(ctx, component, requirement, kinds, command, since)
    times, attempts = ctx.limits
    why = blocked(sequence, times, attempts)
    if why:
        return [Command("BlockCycle", "beck", {"component": component, "requirement": requirement, "reason": why}, event.stream)]
    reason = event.payload.get("reason") or "; ".join(event.payload.get("errors", []))
    return [turn(len(sequence) + 1, reason)]


def _agent_turn(ctx: Ctx, rejection: Event) -> tuple[str, dict[str, Any]]:
    """The command name and args of the agent's turn a rejection is about.

    A SignalRejected answers the turn directly; a verifier's rejection answers the declaration, which
    answers the turn.
    """
    issued = _issued(ctx, rejection)
    return issued.payload["command"], issued.payload["args"]


def _declared_test(ctx: Ctx, component: str, requirement: str) -> tuple[str, str]:
    declared = [e for e in ctx.events(stream=stream(component, requirement), types=("RedVerified",))][-1]
    unit = _issued(ctx, declared).payload["args"]["unit"]       # RedVerified ← RedDeclared ← the red turn
    return declared.payload["test"], unit


# --- translators: the agent's JSON into events -----------------------------------


@cycle.translator("WriteRedTest")
def red_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    args = command.payload["args"]
    if output["outcome"] == "change_requested":
        return [_change(args, output)]
    return [Result("RedDeclared", {"component": args["component"], "requirement": args["requirement"],
                                   "test": output["result"]["test"]},
                   actor="unclebob")]


@cycle.translator("WriteGreenCode")
def green_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    args = command.payload["args"]
    if output["outcome"] == "change_requested":
        return [_change(args, output)]
    return [Result("GreenDeclared", {"component": args["component"], "requirement": args["requirement"], "test": args["test"]},
                   actor="unclebob")]


def _change(args: dict[str, Any], output: dict[str, Any]) -> Result:
    return Result("ChangeRequested", {"agent": "unclebob", "component": args["component"], "requirement": args["requirement"],
                                      **output["change"]}, actor="unclebob")


# --- handlers: beck's deterministic steps --------------------------------------------


def _runs(ctx: Ctx, turn: str) -> list[Event]:
    """The turn's runs, after those of the turns it retries for a malformed answer alone (SignalRejected):
    the retry fixes the answer, not the code, so the earlier run stands while the tree is the one it saw."""
    chain = [ctx.store.get(turn)]
    while chain[-1].causation_id:
        cause = ctx.store.get(chain[-1].causation_id)
        if cause.type != "SignalRejected" or not cause.causation_id:
            break
        chain.append(ctx.store.get(cause.causation_id))
    return sorted((e for t in chain for e in ctx.store.caused_by(t.id) if e.type == "TestsRun"),
                  key=lambda e: e.seq)


@cycle.handler("VerifyRed")
def handle_verify_red(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    # a test that passes proves the requirement when its code exists: the integration component's (over components already
    # implemented), or a requirement the index shows implemented by an earlier green
    met = getattr(ctx.workspace, "already_met", None)
    accept = a["component"] == INTEGRATION or bool(met and met(a["component"], a["requirement"]))
    verdict = verify.verify_red(ctx.workspace.worktree(a["component"]), _runs(ctx, a["turn"]), a["test"], ctx.project,
                                accept_pass=accept)
    if verdict.ok:
        return [Result("RedVerified", {"component": a["component"], "requirement": a["requirement"], "test": a["test"],
                                       "run": verdict.run or "",
                                       **({"already_green": True} if verdict.reason == "already green" else {})})]
    return [Result("RedRejected", {"component": a["component"], "requirement": a["requirement"], "reason": verdict.reason,
                                   "signature": signature(verdict.problems)})]


@cycle.handler("VerifyGreen")
def handle_verify_green(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    verdict = verify.verify_green(ctx.workspace.worktree(a["component"]), _runs(ctx, a["turn"]), a["test"],
                                  ctx.workspace.scope(a["component"]), ctx.project)
    if verdict.ok:
        return [Result("GreenVerified", {"component": a["component"], "requirement": a["requirement"], "test": a["test"],
                                         "run": verdict.run or ""})]
    payload = {"component": a["component"], "requirement": a["requirement"], "reason": verdict.reason,
               "signature": signature(verdict.problems)}
    if verdict.failure:
        payload["failure"] = verdict.failure
    return [Result("GreenRejected", payload)]


def commit(root: Path, message: str, allow_empty: bool = False, empty: bool = False) -> str:
    """Commits everything outside .beck/; tolerates a commit already made before a crash. allow_empty: a green
    may need no code (a cycle reopened on a test that passes as it is): nothing is committed, HEAD stands."""
    git = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    git("add", "-A", "--", ".", ":!.beck")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode != 0
    if staged:
        git("commit", "-q", "-m", message)
    elif empty and git("log", "-1", "--format=%s") != message:   # once: a crash may come after it
        git("commit", "-q", "--allow-empty", "-m", message)
    elif git("log", "-1", "--format=%s") != message and not allow_empty:
        raise RuntimeError(f"nothing to commit for {message!r}")
    return git("rev-parse", "HEAD")


def message(ctx: Ctx, component: str, own: str) -> str:
    """A cycle's commit: its own message, or a fixup of the component's commit once the component is squashed."""
    squashed = list(ctx.store.read(stream=f"component:{component}", types_=("ComponentSquashed",)))
    return f"fixup! {squashed[-1].payload['subject']}" if squashed else own


@cycle.handler("CommitRed")
def handle_commit_red(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    tree = ctx.workspace.worktree(a["component"])
    sha = commit(tree, message(ctx, a["component"], f"test({a['component']}): {a['requirement']}"))
    committed = Result("RedCommitted", {"component": a["component"], "requirement": a["requirement"], "sha": sha})
    if a.get("already_green"):   # the code exists: an empty green keeps the TDD order of the commits
        green = commit(tree, message(ctx, a["component"], f"feat({a['component']}): {a['requirement']}"), empty=True)
        return [committed, Result("GreenCommitted", {"component": a["component"], "requirement": a["requirement"], "sha": green}),
                Result("CycleClosed", {"component": a["component"], "requirement": a["requirement"]})]
    return [committed]


def undo_red(tree: Path, component: str, requirement: str) -> None:
    """Takes the cycle's red commit away, found by its subject (a rebase rewrites the sha). Nothing to do when
    there is none (a crash between the undo and its event) or when the requirement was greened: later cycles may
    build on its test. A revert that conflicts is given up, the tree left as it was."""
    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=tree, capture_output=True, text=True, check=check)
    def last(subject: str) -> str:
        return git("log", "-1", "--format=%H", "--fixed-strings", f"--grep={subject}").stdout.strip()
    found = last(f"test({component}): {requirement}")
    if not found or last(f"feat({component}): {requirement}"):
        return
    if found == git("rev-parse", "HEAD").stdout.strip():
        git("reset", "-q", "--hard", "HEAD~1")
    elif git("revert", "--no-edit", found, check=False).returncode != 0:
        git("revert", "--abort", check=False)


@cycle.handler("RewindRed")
def handle_rewind_red(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    tree = ctx.workspace.worktree(a["component"])
    undo_red(tree, a["component"], a["requirement"])   # by its subject: a resume's rebase rewrote its sha
    subprocess.run(["git", "clean", "-fdq", "--", ".", ":!.beck"], cwd=tree, check=True)
    return [Result("RedRewound", {k: a[k] for k in ("component", "requirement", "unit", "reason")})]


@cycle.handler("CommitGreen")
def handle_commit_green(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    sha = commit(ctx.workspace.worktree(a["component"]), message(ctx, a["component"],
                                                                 f"feat({a['component']}): {a['requirement']}"),
                 allow_empty=True)
    return [Result("GreenCommitted", {"component": a["component"], "requirement": a["requirement"], "sha": sha}),
            Result("CycleClosed", {"component": a["component"], "requirement": a["requirement"]})]


@cycle.handler("BlockCycle")
def handle_block(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("CycleBlocked", {"component": a["component"], "requirement": a["requirement"], "reason": a["reason"]})]
