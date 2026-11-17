"""The flakiness detector: a test whose results change on the same code (README, "Flaky tests").

Like the repetition rule that blocks a cycle, it reads the runs in the
events: every TestsRun records the git tree it ran on and the tests that
passed and did not. A test and its code unchanged (the same tree) must give
the same result; when the last runs of a test on one tree (the window, 3)
are mixed, beck hunts, escalating only while it is not sure:

    TestsRun [mixed on one tree] → HuntFlakiness alone (the test by itself, N times)
      → FlakyTestFound [mixed] | FlakinessUnsure → HuntFlakiness component (inside its component's tests, N times:
        a dependency on time or order) → FlakyTestFound | FlakinessUnsure
        → ApproveSuiteRuns (the user: the whole suite, N times, is expensive)
          → SuiteRunsApproved → HuntFlakiness suite → FlakyTestFound | FlakinessCleared
          | SuiteRunsRefused → FlakinessUnresolved

The hunt runs in a worktree of its own, detached at the tree, so the agents
go on meanwhile; its runs are not evidence for the detector. A flaky test of
a component reaches martin as a weak test at the review; every flaky test is in
the notes of the presentation. Nothing stops.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from beck.events.store import Event
from beck.machine.core import Command, Ctx, Issued, Machine, Result
from beck.testrun import run as run_tests
from beck.toolchain import load

flaky = Machine()

# the state machine of a test suspected of flakiness (flaky:<test>): unsure while the hunt widens, then found,
# cleared or left unresolved
F = "flaky:"
flaky.moves(F, "FlakinessUnsure", at=("new", "unsure"), to="unsure")
flaky.moves(F, "SuiteRunsApproved", "SuiteRunsRefused", at=("unsure",))
flaky.moves(F, "FlakyTestFound", at=("new", "unsure"), to="found")
flaky.moves(F, "FlakinessCleared", at=("unsure",), to="cleared")
flaky.moves(F, "FlakinessUnresolved", at=("unsure",), to="unresolved")
flaky.finals(F, "found", "cleared", "unresolved")
flaky.runs(F, "HuntFlakiness", "new", "unsure")
STAGES = ("alone", "component", "suite")


def stream(test: str) -> str:
    return f"flaky:{test}"


def _outcome(run: Event, test: str) -> str | None:
    if test in run.payload.get("passed", []):
        return "passed"
    if test in run.payload.get("not_passed", []):
        return "failed"
    return None


def _hunting(ctx: Ctx, run: Event) -> bool:
    return bool(run.payload.get("hunt"))


@flaky.policy("TestsRun")
def detect(event: Event, ctx: Ctx) -> list[Command]:
    tree = event.payload.get("tree")
    if not tree or _hunting(ctx, event):
        return []
    window = ctx.flakiness[0]
    same_tree = [e for e in ctx.events(types=("TestsRun",)) if e.payload.get("tree") == tree and not _hunting(ctx, e)]
    hunted = {e.payload["test"] for e in ctx.events(types=("FlakyTestFound", "FlakinessUnsure", "FlakinessCleared",
                                                            "FlakinessUnresolved"))}
    commands = []
    for test in [*event.payload.get("not_passed", []), *event.payload.get("passed", [])]:
        if test in hunted:
            continue
        outcomes = [o for o in (_outcome(e, test) for e in same_tree) if o is not None][-window:]
        if len(set(outcomes)) > 1:
            commands.append(Command("HuntFlakiness", "beck", {
                "test": test, "tree": tree, "component": event.payload.get("component") or _component_of(ctx, test),
                "stage": "alone", "seen": outcomes}, stream(test)))
    return commands


def _component_of(ctx: Ctx, test: str) -> str | None:
    for e in ctx.events(types=("RedVerified", "CoverageTestsVerified")):
        tests = [e.payload["test"]] if e.type == "RedVerified" else [t["test"] for t in e.payload["tests"]]
        if test in tests:
            return str(e.payload["component"])
    return None


def _component_tests(ctx: Ctx, component: str) -> list[str]:
    from beck.machine.refactor import _declared_tests
    return list(_declared_tests(ctx, component))


@flaky.handler("HuntFlakiness")
def handle_hunt(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    test, stage, runs = a["test"], a["stage"], ctx.flakiness[1]
    selection = {"alone": [test], "component": _component_tests(ctx, a.get("component") or ""), "suite": []}[stage]
    path = ctx.project / ".beck" / "worktrees" / f"flaky-{command.id[:8]}"
    sha = subprocess.run(["git", "commit-tree", a["tree"], "-m", f"beck: the tree {test} ran on"], cwd=ctx.project,
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(path), sha], cwd=ctx.project,
                   capture_output=True, check=True)
    outcomes: list[str | None] = []
    try:
        toolchain = load(ctx.project)
        for _ in range(runs):
            result = run_tests(path, toolchain, selection, actor="beck", store_root=ctx.project, store=ctx.store,
                               cause=command.cause, hunt=True)
            outcomes.append(_outcome(result.recorded, test))
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=ctx.project, capture_output=True)
        shutil.rmtree(path, ignore_errors=True)
    seen = [o for o in outcomes if o is not None]
    evidence = f"{stage}: {seen.count('passed')} of {len(seen)} runs passed, on one tree"
    base: dict[str, Any] = {"test": test, "stage": stage, "evidence": evidence, "tree": a["tree"]}
    if a.get("component"):
        base["component"] = a["component"]
    if len(set(seen)) > 1:
        return [Result("FlakyTestFound", base, stream=stream(test))]
    if stage == "suite":
        return [Result("FlakinessCleared", base, stream=stream(test))]
    return [Result("FlakinessUnsure", base, stream=stream(test))]


@flaky.policy("FlakinessUnsure")
def escalate(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    hunt = {"test": p["test"], "tree": p["tree"], "component": p.get("component"), "stage": p["stage"]}
    if p["stage"] == "alone" and p.get("component") and _component_tests(ctx, p["component"]):
        return [Command("HuntFlakiness", "beck", {**hunt, "stage": "component"}, event.stream)]
    return [Command("ApproveSuiteRuns", "user", {"test": p["test"], "runs": ctx.flakiness[1], "evidence": p["evidence"],
                                                 "hunt": hunt}, event.stream)]


@flaky.policy("SuiteRunsApproved", "SuiteRunsRefused")
def after_permission(event: Event, ctx: Ctx) -> list[Command]:
    ask = ctx.store.get(event.causation_id or "").payload["args"]
    if event.type == "SuiteRunsApproved":
        return [Command("HuntFlakiness", "beck", {**ask["hunt"], "stage": "suite"}, event.stream)]
    return [Command("LeaveFlakiness", "beck", {"test": ask["test"], "evidence": ask["evidence"]}, event.stream)]


@flaky.handler("LeaveFlakiness")
def handle_leave(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("FlakinessUnresolved", {"test": a["test"], "evidence": a["evidence"]})]


def flaky_tests(ctx: Ctx) -> dict[str, str]:
    """test → the evidence, of every test found flaky."""
    return {e.payload["test"]: e.payload["evidence"] for e in ctx.events(types=("FlakyTestFound",))}

