"""The machine of a cycle: one clause, one test, one red and one green (README, phase 4).

    CycleOpened → WriteRedTest (unclebob) → RedDeclared → VerifyRed → RedVerified → CommitRed → RedCommitted
      → WriteGreenCode (unclebob) → GreenDeclared → VerifyGreen → GreenVerified → CommitGreen
      → GreenCommitted, CycleClosed

A rejection (RedRejected, GreenRejected, a SignalRejected of the turn) sends
the turn again with the reason, until the repetition rule blocks the cycle
(CycleBlocked). An agent's change request (ChangeRequested) ends the turn;
the unit's machine parks the unit. While a unit is parked no turn of its
cycles starts; CycleReopened resumes a cycle at its phase.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beck.events.store import Event
from beck.machine import verify
from beck.machine.core import Command, Ctx, Machine, Result
from beck.machine.signatures import blocked, signature

cycle = Machine()

RED = ("RedRejected",)
GREEN = ("GreenRejected",)


def stream(unit: str, clause: str) -> str:
    return f"cycle:{unit}/{clause}"


@dataclass(frozen=True)
class Workspace:
    """Where a unit lives and what it may touch, given by hand (the tests); beck.machine.unit derives it."""
    worktrees: dict[str, Path]
    scopes: dict[str, list[str]]
    groups: dict[tuple[str, str], dict[str, Any]]   # (unit, group) → beck index context

    def worktree(self, unit: str) -> Path:
        return self.worktrees[unit]

    def scope(self, unit: str) -> list[str]:
        return self.scopes[unit]

    def group_context(self, unit: str, group: str) -> dict[str, Any]:
        return self.groups[(unit, group)]


def parked(ctx: Ctx, unit: str) -> bool:
    """Whether the unit is parked, as of the policy's event."""
    state = False
    for e in ctx.events(stream=f"unit:{unit}", types=("UnitParked", "UnitResumed")):
        state = e.type == "UnitParked"
    return state


def _rejections(ctx: Ctx, unit: str, clause: str, kinds: tuple[str, ...], command: str, since: str) -> list[str]:
    """The signatures of the loop's rejections since its start event, in order."""
    events = ctx.events(stream=stream(unit, clause))
    start = max((e.seq for e in events if e.type == since), default=0)
    return [e.payload["signature"] for e in events if e.seq >= start and (
        e.type in kinds or (e.type == "SignalRejected" and e.payload.get("command") == command))]


def _red_turn(ctx: Ctx, unit: str, clause: str, group: str, first: bool, retry: tuple[int, str] | None) -> Command:
    context: dict[str, Any] = {"clause": clause,
                               "run": f"beck test <the test> --unit {unit} --clause {clause}"}
    if first and retry is None:
        context["group"] = ctx.workspace.group_context(unit, group)
    args: dict[str, Any] = {"session": f"{unit}/{group}", "context": context, "unit": unit, "clause": clause,
                            "group": group}
    if retry:
        args["retry"] = list(retry)
    return Command("WriteRedTest", "unclebob", args, stream(unit, clause))


def _green_turn(unit: str, clause: str, group: str, test: str, failure: str | None,
                retry: tuple[int, str] | None) -> Command:
    context: dict[str, Any] = {"clause": clause, "test": test,
                               "run": f"beck test {test} --unit {unit} --clause {clause}"}
    if failure:
        context["failure"] = failure
    args: dict[str, Any] = {"session": f"{unit}/{group}", "context": context, "unit": unit, "clause": clause,
                            "group": group, "test": test}
    if retry:
        args["retry"] = list(retry)
    return Command("WriteGreenCode", "unclebob", args, stream(unit, clause))


def _issued(ctx: Ctx, event: Event) -> Event:
    """The CommandIssued an executor's result answers."""
    assert event.causation_id is not None
    return ctx.store.get(event.causation_id)


# --- policies ----------------------------------------------------------------


@cycle.policy("CycleOpened")
def open_red(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if parked(ctx, p["unit"]):
        return []
    return [_red_turn(ctx, p["unit"], p["clause"], p["group"], p["first"], None)]


@cycle.policy("CycleReopened")
def reopen(event: Event, ctx: Ctx) -> list[Command]:
    """After a resolution: the cycle goes on at its phase, the reason in the retry."""
    p = event.payload
    retry = (2, p["reason"])
    if p["phase"] == "red":
        return [_red_turn(ctx, p["unit"], p["clause"], p["group"], False, retry)]
    test, group = _declared_test(ctx, p["unit"], p["clause"])
    return [_green_turn(p["unit"], p["clause"], group, test, None, retry)]


@cycle.policy("RedDeclared")
def verify_red(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [Command("VerifyRed", "beck", {**p, "turn": event.causation_id}, event.stream)]


@cycle.policy("RedRejected", "SignalRejected")
def retry_red(event: Event, ctx: Ctx) -> list[Command]:
    name, a = _agent_turn(ctx, event)
    if name != "WriteRedTest" or parked(ctx, a["unit"]):
        return []
    return _retry(ctx, event, a["unit"], a["clause"], RED, name, "CycleOpened",
                  lambda attempt, reason: _red_turn(ctx, a["unit"], a["clause"], a["group"], False, (attempt, reason)))


@cycle.policy("RedVerified")
def commit_red(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitRed", "beck", dict(event.payload), event.stream)]


@cycle.policy("RedCommitted")
def open_green(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if parked(ctx, p["unit"]):
        return []
    test, group = _declared_test(ctx, p["unit"], p["clause"])
    return [_green_turn(p["unit"], p["clause"], group, test, None, None)]


@cycle.policy("GreenDeclared")
def verify_green(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifyGreen", "beck", {**event.payload, "turn": event.causation_id}, event.stream)]


@cycle.policy("GreenRejected", "SignalRejected")
def retry_green(event: Event, ctx: Ctx) -> list[Command]:
    name, a = _agent_turn(ctx, event)
    if name != "WriteGreenCode" or parked(ctx, a["unit"]):
        return []
    failure = event.payload.get("failure")
    return _retry(ctx, event, a["unit"], a["clause"], GREEN, name, "RedCommitted",
                  lambda attempt, reason: _green_turn(a["unit"], a["clause"], a["group"], a["test"], failure,
                                                      (attempt, reason)))


def green_stuck(ctx: Ctx, event: Event) -> bool:
    """A ChangeRequested that takes the cycle back to red: unclebob's `test` kind, or the repetition rule's
    times of change requests from the green of one red (no resolution could unblock it: the red is suspect)."""
    p = event.payload
    if p["agent"] != "unclebob" or "clause" not in p:
        return False
    if p["kind"] == "test":
        return True
    events = ctx.events(stream=stream(p["unit"], p["clause"]))
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
    rewinds = len(ctx.events(stream=stream(a["unit"], a["clause"]), types=("RedRewound",)))
    if rewinds >= ctx.limits[0]:
        return [Command("BlockCycle", "beck", {"unit": a["unit"], "clause": a["clause"],
                                               "reason": f"the red test was found wrong {rewinds} times"},
                        event.stream)]
    why = "the red test is wrong" if p["kind"] == "test" else "the green asked for a change too many times"
    return [Command("RewindRed", "beck", {"unit": a["unit"], "clause": a["clause"], "group": a["group"],
                                          "reason": f"{why}: {p['problem']}"}, event.stream)]


@cycle.policy("RedRewound")
def red_again(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if parked(ctx, p["unit"]):
        return []
    attempt = len(ctx.events(stream=stream(p["unit"], p["clause"]), types=("RedRewound",))) + 1
    return [_red_turn(ctx, p["unit"], p["clause"], p["group"], False, (attempt, p["reason"]))]


@cycle.policy("GreenVerified")
def commit_green(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitGreen", "beck", dict(event.payload), event.stream)]


def _retry(ctx: Ctx, event: Event, unit: str, clause: str, kinds: tuple[str, ...], command: str, since: str,
           turn: Any) -> list[Command]:
    sequence = _rejections(ctx, unit, clause, kinds, command, since)
    times, attempts = ctx.limits
    why = blocked(sequence, times, attempts)
    if why:
        return [Command("BlockCycle", "beck", {"unit": unit, "clause": clause, "reason": why}, event.stream)]
    reason = event.payload.get("reason") or "; ".join(event.payload.get("errors", []))
    return [turn(len(sequence) + 1, reason)]


def _agent_turn(ctx: Ctx, rejection: Event) -> tuple[str, dict[str, Any]]:
    """The command name and args of the agent's turn a rejection is about.

    A SignalRejected answers the turn's CommandIssued directly; a verifier's
    rejection answers the VerifyRed/VerifyGreen command, whose args name the turn.
    """
    issued = _issued(ctx, rejection)
    if rejection.type != "SignalRejected":
        issued = ctx.store.get(issued.payload["args"]["turn"])
    return issued.payload["command"], issued.payload["args"]


def _declared_test(ctx: Ctx, unit: str, clause: str) -> tuple[str, str]:
    declared = [e for e in ctx.events(stream=stream(unit, clause), types=("RedVerified",))][-1]
    turn = ctx.store.get(declared.causation_id or "")           # VerifyRed
    group = ctx.store.get(turn.payload["args"]["turn"]).payload["args"]["group"]
    return declared.payload["test"], group


# --- translators: the agent's JSON into events -----------------------------------


@cycle.translator("WriteRedTest")
def red_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    args = command.payload["args"]
    if output["outcome"] == "change_requested":
        return [_change(args, output)]
    return [Result("RedDeclared", {"unit": args["unit"], "clause": args["clause"], "test": output["result"]["test"]},
                   actor="unclebob")]


@cycle.translator("WriteGreenCode")
def green_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    args = command.payload["args"]
    if output["outcome"] == "change_requested":
        return [_change(args, output)]
    return [Result("GreenDeclared", {"unit": args["unit"], "clause": args["clause"], "test": args["test"]},
                   actor="unclebob")]


def _change(args: dict[str, Any], output: dict[str, Any]) -> Result:
    return Result("ChangeRequested", {"agent": "unclebob", "unit": args["unit"], "clause": args["clause"],
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
def handle_verify_red(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    verdict = verify.verify_red(ctx.workspace.worktree(a["unit"]), _runs(ctx, a["turn"]), a["test"], ctx.project)
    if verdict.ok:
        return [Result("RedVerified", {"unit": a["unit"], "clause": a["clause"], "test": a["test"],
                                       "run": verdict.run or ""})]
    return [Result("RedRejected", {"unit": a["unit"], "clause": a["clause"], "reason": verdict.reason,
                                   "signature": signature(verdict.problems)})]


@cycle.handler("VerifyGreen")
def handle_verify_green(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    verdict = verify.verify_green(ctx.workspace.worktree(a["unit"]), _runs(ctx, a["turn"]), a["test"],
                                  ctx.workspace.scope(a["unit"]), ctx.project)
    if verdict.ok:
        return [Result("GreenVerified", {"unit": a["unit"], "clause": a["clause"], "test": a["test"],
                                         "run": verdict.run or ""})]
    payload = {"unit": a["unit"], "clause": a["clause"], "reason": verdict.reason,
               "signature": signature(verdict.problems)}
    if verdict.failure:
        payload["failure"] = verdict.failure
    return [Result("GreenRejected", payload)]


def commit(root: Path, message: str) -> str:
    """Commits everything outside .beck/; tolerates a commit already made before a crash."""
    git = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    git("add", "-A", "--", ".", ":!.beck")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode != 0
    if staged:
        git("commit", "-q", "-m", message)
    elif git("log", "-1", "--format=%s") != message:
        raise RuntimeError(f"nothing to commit for {message!r}")
    return git("rev-parse", "HEAD")


def message(ctx: Ctx, unit: str, own: str) -> str:
    """A cycle's commit: its own message, or a fixup of the unit's commit once the unit is squashed."""
    squashed = list(ctx.store.read(stream=f"unit:{unit}", types_=("UnitSquashed",)))
    return f"fixup! {squashed[-1].payload['subject']}" if squashed else own


@cycle.handler("CommitRed")
def handle_commit_red(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    sha = commit(ctx.workspace.worktree(a["unit"]), message(ctx, a["unit"], f"test({a['unit']}): {a['clause']}"))
    return [Result("RedCommitted", {"unit": a["unit"], "clause": a["clause"], "sha": sha})]


def undo_red(tree: Path, unit: str, clause: str) -> None:
    """Takes the cycle's red commit away, found by its subject (a rebase rewrites the sha). Nothing to do when
    there is none (a crash between the undo and its event) or when the clause was greened: later cycles may
    build on its test. A revert that conflicts is given up, the tree left as it was."""
    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=tree, capture_output=True, text=True, check=check)
    def last(subject: str) -> str:
        return git("log", "-1", "--format=%H", "--fixed-strings", f"--grep={subject}").stdout.strip()
    found = last(f"test({unit}): {clause}")
    if not found or last(f"feat({unit}): {clause}"):
        return
    if found == git("rev-parse", "HEAD").stdout.strip():
        git("reset", "-q", "--hard", "HEAD~1")
    elif git("revert", "--no-edit", found, check=False).returncode != 0:
        git("revert", "--abort", check=False)


@cycle.handler("RewindRed")
def handle_rewind_red(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    tree = ctx.workspace.worktree(a["unit"])
    undo_red(tree, a["unit"], a["clause"])   # by its subject: a resume's rebase rewrote its sha
    subprocess.run(["git", "clean", "-fdq", "--", ".", ":!.beck"], cwd=tree, check=True)
    return [Result("RedRewound", {k: a[k] for k in ("unit", "clause", "group", "reason")})]


@cycle.handler("CommitGreen")
def handle_commit_green(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    sha = commit(ctx.workspace.worktree(a["unit"]), message(ctx, a["unit"], f"feat({a['unit']}): {a['clause']}"))
    return [Result("GreenCommitted", {"unit": a["unit"], "clause": a["clause"], "sha": sha}),
            Result("CycleClosed", {"unit": a["unit"], "clause": a["clause"]})]


@cycle.handler("BlockCycle")
def handle_block(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("CycleBlocked", {"unit": a["unit"], "clause": a["clause"], "reason": a["reason"]})]
