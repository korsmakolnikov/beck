"""The flakiness detector: a test whose results change on the same code (README, "Flaky tests").

Like the repetition rule that blocks a cycle, it reads the runs in the
events: every TestsRun records the git tree it ran on and the tests that
passed and did not. A test and its code unchanged (the same tree) must give
the same result; when the last runs of a test on one tree (the window, 3)
are mixed, beck hunts, escalating only while it is not sure:

    TestsRun [mixed on one tree] → HuntFlakiness alone (the test by itself, N times)
      → FlakyTestFound [mixed] | FlakinessUnsure → HuntFlakiness unit (inside its unit's tests, N times:
        a dependency on time or order) → FlakyTestFound | FlakinessUnsure
        → ApproveSuiteRuns (the user: the whole suite, N times, is expensive)
          → SuiteRunsApproved → HuntFlakiness suite → FlakyTestFound | FlakinessCleared
          | SuiteRunsRefused → FlakinessUnresolved

The hunt runs in a worktree of its own, detached at the tree, so the agents
go on meanwhile; its runs are not evidence for the detector. A flaky test of
a unit reaches martin as a weak test at the review; every flaky test is in
the notes of the presentation. Nothing stops.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from beck.events.store import Event
from beck.machine.core import Command, Ctx, Machine, Result
from beck.testrun import run as run_tests
from beck.toolchain import load

flaky = Machine()
STAGES = ("alone", "unit", "suite")


def stream(test: str) -> str:
    return f"flaky:{test}"


def _outcome(run: Event, test: str) -> str | None:
    if test in run.payload.get("passed", []):
        return "passed"
    if test in run.payload.get("not_passed", []):
        return "failed"
    return None


def _hunting(ctx: Ctx, run: Event) -> bool:
    cause = ctx.store.get(run.causation_id) if run.causation_id else None
    return cause is not None and cause.type == "CommandIssued" and cause.payload["command"] == "HuntFlakiness"


@flaky.policy("TestsRun")
def detect(event: Event, ctx: Ctx) -> list[Command]:
    tree = event.payload.get("tree")
    if not tree or _hunting(ctx, event):
        return []
    window = ctx.flakiness[0]
    same_tree = [e for e in ctx.events(types=("TestsRun",)) if e.payload.get("tree") == tree and not _hunting(ctx, e)]
    hunted = {e.payload["args"]["test"] for e in ctx.events(types=("CommandIssued",))
              if e.payload["command"] == "HuntFlakiness"}
    commands = []
    for test in [*event.payload.get("not_passed", []), *event.payload.get("passed", [])]:
        if test in hunted:
            continue
        outcomes = [o for o in (_outcome(e, test) for e in same_tree) if o is not None][-window:]
        if len(set(outcomes)) > 1:
            commands.append(Command("HuntFlakiness", "beck", {
                "test": test, "tree": tree, "unit": event.payload.get("unit") or _unit_of(ctx, test),
                "stage": "alone", "seen": outcomes}, stream(test)))
    return commands


def _unit_of(ctx: Ctx, test: str) -> str | None:
    for e in ctx.events(types=("RedVerified", "CoverageTestsVerified")):
        tests = [e.payload["test"]] if e.type == "RedVerified" else [t["test"] for t in e.payload["tests"]]
        if test in tests:
            return str(e.payload["unit"])
    return None


def _unit_tests(ctx: Ctx, unit: str) -> list[str]:
    from beck.machine.refactor import _declared_tests
    return list(_declared_tests(ctx, unit))


@flaky.handler("HuntFlakiness")
def handle_hunt(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    test, stage, runs = a["test"], a["stage"], ctx.flakiness[1]
    selection = {"alone": [test], "unit": _unit_tests(ctx, a.get("unit") or ""), "suite": []}[stage]
    path = ctx.project / ".beck" / "worktrees" / f"flaky-{command.id[:8]}"
    sha = subprocess.run(["git", "commit-tree", a["tree"], "-m", f"beck: the tree {test} ran on"], cwd=ctx.project,
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(path), sha], cwd=ctx.project,
                   capture_output=True, check=True)
    outcomes: list[str | None] = []
    try:
        toolchain = load(ctx.project)
        for _ in range(runs):
            result = run_tests(path, toolchain, selection, actor="beck", store_root=ctx.project, cause_id=command.id,
                               iteration=ctx.iteration)
            outcomes.append(_outcome(result.event, test))
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=ctx.project, capture_output=True)
        shutil.rmtree(path, ignore_errors=True)
    seen = [o for o in outcomes if o is not None]
    evidence = f"{stage}: {seen.count('passed')} of {len(seen)} runs passed, on one tree"
    base: dict[str, Any] = {"test": test, "stage": stage, "evidence": evidence}
    if a.get("unit"):
        base["unit"] = a["unit"]
    if len(set(seen)) > 1:
        return [Result("FlakyTestFound", base, stream=stream(test))]
    if stage == "suite":
        return [Result("FlakinessCleared", base, stream=stream(test))]
    return [Result("FlakinessUnsure", base, stream=stream(test))]


@flaky.policy("FlakinessUnsure")
def escalate(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    hunt = ctx.store.get(event.causation_id or "").payload["args"]
    if p["stage"] == "alone" and p.get("unit") and _unit_tests(ctx, p["unit"]):
        return [Command("HuntFlakiness", "beck", {**hunt, "stage": "unit"}, event.stream)]
    return [Command("ApproveSuiteRuns", "user", {"test": p["test"], "runs": ctx.flakiness[1], "evidence": p["evidence"],
                                                 "hunt": hunt}, event.stream)]


@flaky.policy("SuiteRunsApproved", "SuiteRunsRefused")
def after_permission(event: Event, ctx: Ctx) -> list[Command]:
    ask = ctx.store.get(event.causation_id or "").payload["args"]
    if event.type == "SuiteRunsApproved":
        return [Command("HuntFlakiness", "beck", {**ask["hunt"], "stage": "suite"}, event.stream)]
    return [Command("LeaveFlakiness", "beck", {"test": ask["test"], "evidence": ask["evidence"]}, event.stream)]


@flaky.handler("LeaveFlakiness")
def handle_leave(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("FlakinessUnresolved", {"test": a["test"], "evidence": a["evidence"]})]


def flaky_tests(ctx: Ctx) -> dict[str, str]:
    """test → the evidence, of every test found flaky."""
    return {e.payload["test"]: e.payload["evidence"] for e in ctx.events(types=("FlakyTestFound",))}

