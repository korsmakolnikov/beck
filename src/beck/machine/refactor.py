"""The machine of phase 5: martin's refactor of a unit, its squash, its landing (README, phase 5).

    UnitImplemented → RunUnitTests (beck) → CoverageMeasured (and CoverageDecreased, reported at the review)
      → UnitVerified | RegressionFound (→ CycleReopened, green)
      | CoverageGapsFound [first time] → CoverLines (unclebob) → CoverageTestsDeclared → VerifyCover
        → CoverageTestsVerified → CommitCoverage → CoverageTestsCommitted → RunUnitTests
        | CoverageRejected ⟲ CoverLines, once | GiveUpCoverage → RunUnitTests (the gaps go to martin)
      | CoverageDecreased → Ask (the user, routed by kent): go-on → UnitVerified | park-unit | cover-again
    UnitVerified [not squashed] → SquashUnit → UnitSquashed → MeasureUnit → UnitMeasured → ReviewUnit (martin) → UnitReviewed
    UnitReviewed | RefactorCommitted [items left] → RefactorUnit (martin: one weak test, or one finding)
      → RefactorDeclared → VerifyRefactor (beck runs the unit's tests, checks the phase and the measures)
      → RefactorVerified → CommitRefactor (a fixup of the unit's commit) → RefactorCommitted
      | RefactorRejected ⟲ RefactorUnit | MeasureWorsened → parked, ApproveTradeoff (the user)
    [no item left] → CheckUnit (beck) → ChecksPassed | ChecksFailed → FixStaticFindings (martin) → RefactorDeclared …
    ChecksPassed → AutosquashUnit → UnitReady
    UnitReady | UnitLanded | ResolutionApplied → LandUnit [its dependencies landed]
      → UnitLanded | LandingDeferred | LandingConflicted → ResolveConflict (martin) → ConflictResolved → ContinueLanding
    UnitLanded → CloseUnit → UnitClosed
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from beck import checks as static_checks
from beck.events.store import Event
from beck.index.anchor import ids_at, is_test_file
from beck.junit import UnreadableReport, parse, summary
from beck.machine.core import Command, Ctx, Machine, Result, action, ask_routed
from beck.machine.cycle import commit, parked
from beck.machine.cycle import stream as cycle_stream
from beck.machine.signatures import blocked, signature
from beck.machine.unit import _park_and_propose, implementation, plan, stream
from beck.machine.verify import changed_files
from beck.measures import coverage_gaps, files_in, measure, ratchet, totals, worsened
from beck.measures.coverage import READERS, FileCoverage
from beck.testrun import run as run_tests
from beck.toolchain import load

refactor = Machine()


def _git(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})})


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _base(ctx: Ctx, unit: str) -> str:
    """The commit the unit's work starts from: its UnitStarted's, or the last rebase's."""
    events = ctx.store.read(stream=stream(unit), types_=("UnitStarted", "UnitResumed", "UnitReopened"))
    return [e.payload["base"] for e in events][-1]


def _subject(ctx: Ctx, unit: str) -> str | None:
    squashed = list(ctx.store.read(stream=stream(unit), types_=("UnitSquashed",)))
    return squashed[-1].payload["subject"] if squashed else None


def _declared_tests(ctx: Ctx, unit: str) -> dict[str, str]:
    """The unit's tests, as the cycles and the coverage declared them: test → clause."""
    tests: dict[str, str] = {}
    for e in ctx.store.read(types_=("RedVerified", "CoverageTestsVerified")):
        if e.payload["unit"] != unit:
            continue
        if e.type == "RedVerified":
            tests[e.payload["test"]] = e.payload["clause"]
        else:
            tests.update({t["test"]: t["clause"] for t in e.payload["tests"]})
    return tests


def _fitness(tree: Path) -> list[str]:
    return [d for d in ("tests/fitness", "test/fitness") if (tree / d).is_dir()]


def _run_unit(ctx: Ctx, unit: str, command: Event, extra: list[str] | None = None) -> tuple[Event, Any]:
    """beck runs the unit's tests and the fitness functions, asked by a policy; returns the run and its report."""
    tree = ctx.workspace.worktree(unit)
    selectors = list(dict.fromkeys([*_declared_tests(ctx, unit), *(extra or [])])) + _fitness(tree)
    result = run_tests(tree, load(ctx.project), selectors, actor="beck", unit=unit, store_root=ctx.project,
                       cause_id=command.id, iteration=ctx.iteration, coverage_of=ctx.workspace.scope(unit))
    report = None
    if "report" in result.event.payload:
        try:
            report = parse(ctx.project / result.event.payload["report"], tree)
        except UnreadableReport:
            report = None
    return result.event, report


def _failure(run: Event, report: Any) -> str:
    if report is None:
        return f"NO_REPORT (exit {run.payload['exit_code']}): see {run.payload['log']}"
    return summary(report)


# --- the unit's tests, then the squash --------------------------------------------------------------


@refactor.policy("UnitImplemented")
def run_unit_tests(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunUnitTests", "beck", {"unit": event.payload["unit"]}, stream(event.payload["unit"]))]


@refactor.handler("RunUnitTests")
def handle_run_unit_tests(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    run, report = _run_unit(ctx, unit, command)
    if report is None or report.not_passed:
        failing = [c.id for c in report.not_passed] if report else []
        return [Result("RegressionFound", {"unit": unit, "tests": failing, "failure": _failure(run, report)})]
    coverage = _coverage_of(ctx, run, ctx.workspace.worktree(unit))
    if coverage is None:
        return [Result("UnitVerified", {"unit": unit})]
    return _judge_coverage(ctx, unit, coverage)


@refactor.policy("RegressionFound")
def reopen_regressions(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [Command("ReopenRegressions", "beck", dict(p), event.stream)]


@refactor.handler("ReopenRegressions")
def handle_reopen(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    unit, tests = a["unit"], _declared_tests(ctx, a["unit"])
    groups = {e.payload["clause"]: e.payload["group"] for e in ctx.store.read(types_=("CycleOpened",))
              if e.payload["unit"] == unit}
    clauses = sorted({tests[t] for t in a["tests"] if t in tests})
    if not clauses:  # a fitness function, or no report: nothing to hand to unclebob
        return [Result("UnitParked", {"unit": unit, "reason": f"the unit's tests fail outside its cycles: "
                                                             f"{a['failure'][:300]}", "because_of": command.id})]
    return [Result("CycleReopened", {"unit": unit, "clause": c, "group": groups[c], "phase": "green",
                                     "reason": f"a regression: {a['failure'][:500]}"}, stream=cycle_stream(unit, c))
            for c in clauses]


# --- the coverage: measured, its gaps tried once, never a stop -----------------------------------


def _coverage_of(ctx: Ctx, run: Event, tree: Path) -> dict[str, FileCoverage] | None:
    if "coverage" not in run.payload:
        return None
    try:
        return READERS[run.payload.get("coverage_format", "coverage.py")](ctx.project / run.payload["coverage"], tree)
    except (OSError, ValueError, KeyError):
        return None


def _baseline(ctx: Ctx) -> dict[str, list[int]]:
    """The project's coverage: the start's suite, then every landed unit's files as they landed."""
    baseline: dict[str, list[int]] = {}
    last: dict[str, dict[str, list[int]]] = {}
    for e in ctx.events(types=("CoverageMeasured", "UnitLanded")):
        if e.type == "CoverageMeasured" and "unit" not in e.payload:
            baseline = dict(e.payload["files"])
        elif e.type == "CoverageMeasured":
            last[e.payload["unit"]] = e.payload["files"]
        elif e.payload["unit"] in last:
            baseline |= last[e.payload["unit"]]
    return baseline


def _judge_coverage(ctx: Ctx, unit: str, coverage: dict[str, FileCoverage]) -> list[Result]:
    tree = ctx.workspace.worktree(unit)
    files = files_in(tree, ctx.workspace.scope(unit))
    sources = [f for f in files if not is_test_file(Path(f))]
    gaps = coverage_gaps(coverage, tree, _base(ctx, unit), sources)
    measured = totals(coverage, sources)
    before, after = ratchet(_baseline(ctx), measured, sources)
    results = [Result("CoverageMeasured", {"unit": unit, "files": measured, "gaps": gaps, "before": before,
                                           "after": after})]
    tried = any(e.payload["command"] == "CoverLines" for e in ctx.events(stream=stream(unit), types=("CommandIssued",)))
    clauses = _clauses_of_gaps(ctx, unit, tree, gaps) if gaps else {}
    if clauses and not tried:
        return results + [Result("CoverageGapsFound", {"unit": unit, "clauses": clauses})]
    if before is not None and after is not None and after < before:   # TDD never lowers it: the user decides
        return results + [Result("CoverageDecreased", {"unit": unit, "before": before, "after": after,
                                                       "clauses": clauses})]
    return results + [Result("UnitVerified", {"unit": unit})]


@refactor.policy("CoverageDecreased")
def ask_about_the_coverage(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    actions = [action("go-on", "Accept the lower coverage: the unit goes on to its review."),
               action("park-unit", "Park the unit; the other units go on.")]
    if p.get("clauses"):
        actions.insert(0, action("cover-again", "Ask unclebob again for the tests that run the lines, "
                                                "with the user's words."))
    measured = [e for e in ctx.events(stream=stream(p["unit"]), types=("CoverageMeasured",))][-1]
    return [ask_routed(f"{p['unit']} lowers the project's coverage from {p['before']} % to {p['after']} %; "
                       f"lines no test runs: {measured.payload['gaps']}",
                       "With TDD the coverage never goes down.", "coverage-decreased", actions, event.stream,
                       event.id)]


@refactor.policy("DecisionRouted")
def after_the_coverage_question(event: Event, ctx: Ctx) -> list[Command]:
    if event.payload["situation"] != "coverage-decreased":
        return []
    decreased = ctx.store.get(ctx.store.get(event.payload["ask"]).payload["args"]["turn"])
    unit, chosen = decreased.payload["unit"], event.payload["action"]
    if chosen == "go-on":
        return [Command("AcceptCoverage", "beck", {"unit": unit}, stream(unit))]
    if chosen == "park-unit":
        return [Command("ParkUnits", "beck", {"units": [unit], "reason": f"the coverage: {event.payload['why']}",
                                              "because_of": event.id}, stream(unit))]
    return [_cover_turn(ctx, unit, decreased.payload["clauses"], (2, f"the user: {event.payload['why']}"))]


@refactor.handler("AcceptCoverage")
def handle_accept_coverage(command: Event, ctx: Ctx) -> list[Result]:
    return [Result("UnitVerified", {"unit": command.payload["args"]["unit"]}, actor="user")]


def _clauses_of_gaps(ctx: Ctx, unit: str, tree: Path, gaps: dict[str, list[int]]) -> dict[str, dict[str, list[int]]]:
    """{clause: {file: lines}}: each line to the clause that names its code, through the `spec:` markers."""
    groups: dict[str, list[str]] = {}
    for e in ctx.store.read(types_=("CycleOpened",)):
        if e.payload["unit"] == unit and e.payload["clause"] not in groups.setdefault(e.payload["group"], []):
            groups[e.payload["group"]].append(e.payload["clause"])
    clauses = {c for cs in groups.values() for c in cs}
    out: dict[str, dict[str, list[int]]] = {}
    for f, lines in gaps.items():
        for line, ids in sorted(ids_at(tree, Path(f), set(lines)).items()):
            clause = next((i for i in ids if i in clauses), None)
            if clause is None:
                clause = next((groups[g][0] for i in ids for g in (f"operation:{i}", f"entity:{i}") if g in groups), None)
            if clause is not None:
                out.setdefault(clause, {}).setdefault(f, []).append(line)
    return out


def _cover_turn(ctx: Ctx, unit: str, clauses: dict[str, dict[str, list[int]]], retry: tuple[int, str] | None
                ) -> Command:
    tests: dict[str, list[str]] = {}
    for test, clause in _declared_tests(ctx, unit).items():
        tests.setdefault(clause, []).append(test)
    context = {"clauses": [{"id": c, "text": ctx.workspace.clause_text(c), "tests": tests.get(c, []), "lines": lines}
                           for c, lines in clauses.items()],
               "run": f"beck test <the tests> --unit {unit}"}
    args: dict[str, Any] = {"session": f"{unit}/coverage", "context": context, "unit": unit, "clauses": clauses}
    if retry:
        args["retry"] = list(retry)
    return Command("CoverLines", "unclebob", args, stream(unit))


@refactor.policy("CoverageGapsFound")
def cover_lines(event: Event, ctx: Ctx) -> list[Command]:
    return [_cover_turn(ctx, event.payload["unit"], event.payload["clauses"], None)]


@refactor.translator("CoverLines")
def coverage_tests_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    if output["outcome"] == "change_requested":   # covering is optional: a change is not worth stopping for
        return [Result("CoverageRejected", {"unit": unit, "reason": "unclebob asked for a change: "
                                            + output["change"]["problem"], "signature": signature(["change"])},
                       actor="unclebob")]
    return [Result("CoverageTestsDeclared", {"unit": unit, "tests": output["result"]["tests"]}, actor="unclebob")]


@refactor.policy("CoverageTestsDeclared")
def verify_cover(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifyCover", "beck", {**event.payload, "turn": event.causation_id}, event.stream)]


@refactor.handler("VerifyCover")
def handle_verify_cover(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    unit, declared = a["unit"], a["tests"]
    tree = ctx.workspace.worktree(unit)
    turn = ctx.store.get(a["turn"])
    clauses = turn.payload["args"]["clauses"]
    changed = changed_files(tree)
    sources = [f for f in changed if not is_test_file(Path(f))]

    def rejected(reason: str, problems: list[str], failure: str | None = None) -> list[Result]:
        payload = {"unit": unit, "reason": reason, "signature": signature(problems)}
        if failure:
            payload["failure"] = failure
        return [Result("CoverageRejected", payload)]

    if sources:
        return rejected(f"covering changes no source; changed: {', '.join(sources)}", sources)
    if not changed:
        return rejected("no test was written", ["nothing"])
    unknown = [t["clause"] for t in declared if t["clause"] not in clauses]
    if unknown:
        return rejected(f"the tests cite clauses outside the request: {', '.join(unknown)}", unknown)
    run, report = _run_unit(ctx, unit, command, [t["test"] for t in declared])
    if report is None or report.not_passed:
        return rejected("the unit's tests do not pass with the new tests",
                        [c.id for c in report.not_passed] if report else ["no-report"], _failure(run, report))
    ran = {c.id for c in report.cases}
    missing = [t["test"] for t in declared if t["test"] not in ran]
    if missing:
        return rejected(f"the declared tests did not run: {', '.join(missing)}", missing)
    coverage = _coverage_of(ctx, run, tree)
    if coverage is not None:
        wanted = {(f, line) for lines in clauses.values() for f, ls in lines.items() for line in ls}
        still = {(f, line) for f, line in wanted if f not in coverage or line in coverage[f].missing}
        if still == wanted:
            return rejected("the new tests run none of the lines", sorted(f"{f}:{n}" for f, n in still))
    return [Result("CoverageTestsVerified", {"unit": unit, "tests": declared})]


@refactor.policy("CoverageTestsVerified")
def commit_cover(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitCoverage", "beck", {"unit": event.payload["unit"],
                                               "clauses": sorted({t["clause"] for t in event.payload["tests"]})},
                    event.stream)]


@refactor.handler("CommitCoverage")
def handle_commit_cover(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    sha = commit(ctx.workspace.worktree(a["unit"]), f"test({a['unit']}): cover {', '.join(a['clauses'])}")
    return [Result("CoverageTestsCommitted", {"unit": a["unit"], "sha": sha})]


@refactor.policy("CoverageTestsCommitted")
def run_again_after_cover(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunUnitTests", "beck", {"unit": event.payload["unit"]}, event.stream)]


@refactor.policy("CoverageRejected", "SignalRejected")
def retry_cover(event: Event, ctx: Ctx) -> list[Command]:
    if event.type == "SignalRejected":
        turn = ctx.store.get(event.causation_id or "")
        if turn.payload["command"] != "CoverLines":
            return []
    else:
        verifier = ctx.store.get(event.causation_id or "")
        turn = ctx.store.get(verifier.payload["args"]["turn"]) if verifier.payload["command"] == "VerifyCover" \
            else verifier
    unit = turn.payload["args"]["unit"]
    attempts = [e for e in ctx.events(stream=stream(unit), types=("CommandIssued",))
                if e.payload["command"] == "CoverLines"]
    revert = Command("RevertRefactor", "beck", {"unit": unit}, stream(unit))
    reason = event.payload.get("reason") or "; ".join(event.payload.get("errors", []))
    if len(attempts) >= 2:
        return [revert, Command("GiveUpCoverage", "beck", {"unit": unit, "reason": reason[:500]}, stream(unit))]
    if event.payload.get("failure"):
        reason = f"{reason}\n{event.payload['failure']}"
    return [revert, _cover_turn(ctx, unit, turn.payload["args"]["clauses"], (2, reason[:2000]))]


@refactor.handler("GiveUpCoverage")
def handle_give_up_coverage(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("CoverageGivenUp", {"unit": a["unit"], "reason": a["reason"]})]


@refactor.policy("CoverageGivenUp")
def judge_again_after_giving_up(event: Event, ctx: Ctx) -> list[Command]:
    """The tests run again without the try: the coverage is judged as it is (no second try)."""
    return [Command("RunUnitTests", "beck", {"unit": event.payload["unit"]}, event.stream)]


@refactor.policy("UnitVerified")
def squash_or_measure(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    name = "MeasureUnit" if _squashed_as_of(ctx, unit) else "SquashUnit"
    return [Command(name, "beck", {"unit": unit}, event.stream)]


def _squashed_as_of(ctx: Ctx, unit: str) -> bool:
    return bool(ctx.events(stream=stream(unit), types=("UnitSquashed",)))


@refactor.handler("SquashUnit")
def handle_squash(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    clauses = list(dict.fromkeys(_declared_tests(ctx, unit).values()))
    subject = f"feat({unit}): implement {unit}"
    if _subject(ctx, unit) is None and _git(tree, "log", "-1", "--format=%s").stdout.strip() != subject:
        _git(tree, "reset", "-q", "--soft", _base(ctx, unit))
        _git(tree, "commit", "-q", "-m", subject, "-m", "Clauses: " + ", ".join(clauses))
    return [Result("UnitSquashed", {"unit": unit, "sha": _head(tree), "subject": subject})]


@refactor.policy("UnitSquashed")
def measure_after_squash(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("MeasureUnit", "beck", {"unit": event.payload["unit"]}, event.stream)]


def _measures(ctx: Ctx, unit: str) -> dict[str, Any]:
    tree = ctx.workspace.worktree(unit)
    toolchain = load(ctx.project)
    return measure(tree, files_in(tree, ctx.workspace.scope(unit)), toolchain.complexity, toolchain)


@refactor.handler("MeasureUnit")
def handle_measure(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    return [Result("UnitMeasured", {"unit": unit, "measures": _measures(ctx, unit)})]


# --- martin's review and refactors ------------------------------------------------------------------


@refactor.policy("UnitMeasured")
def review(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    tree = ctx.workspace.worktree(unit)
    tests: dict[str, list[str]] = {}
    for test, clause in _declared_tests(ctx, unit).items():
        tests.setdefault(clause, []).append(test)
    clauses = [{"id": c, "text": ctx.workspace.clause_text(c), "tests": ts} for c, ts in tests.items()]
    context = {"unit": {"id": unit, "namespace": [], "files": files_in(tree, ctx.workspace.scope(unit))},
               "measures": event.payload["measures"], "clauses": clauses}
    measured = [e for e in ctx.events(stream=stream(unit), types=("CoverageMeasured",))]
    if measured and measured[-1].payload["gaps"]:
        context["uncovered"] = measured[-1].payload["gaps"]
    flaky = [{"test": e.payload["test"], "evidence": e.payload["evidence"]}
             for e in ctx.events(types=("FlakyTestFound",)) if e.payload["test"] in _declared_tests(ctx, unit)]
    if flaky:
        context["flaky"] = flaky
    return [Command("ReviewUnit", "martin", {"session": f"martin/{unit}", "context": context, "unit": unit},
                    event.stream)]


@refactor.translator("ReviewUnit")
def unit_reviewed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "unit": unit, **output["change"]}, actor="martin")]
    return [Result("UnitReviewed", {"unit": unit, **output["result"]}, actor="martin")]


def _items_left(ctx: Ctx, unit: str) -> list[dict[str, Any]]:
    """The weak tests, then the findings, of the last review that no refactor addressed yet."""
    events = ctx.events(stream=stream(unit))
    reviews = [e for e in events if e.type == "UnitReviewed"]
    if not reviews:
        return []
    last = reviews[-1]
    done = {a for e in events if e.type == "RefactorCommitted" and e.seq > last.seq for a in e.payload["addressed"]}
    items = [{"weak_tests": [w]} for w in last.payload["weak_tests"] if w["test"] not in done]
    return items + [{"findings": [f]} for f in last.payload["findings"] if f["id"] not in done]


def _refactor_turn(unit: str, item: dict[str, Any], retry: tuple[int, str] | None) -> Command:
    context = {**item, "run": f"beck test --unit {unit}", "check": f"beck check --unit {unit}"}
    args: dict[str, Any] = {"session": f"martin/{unit}", "context": context, "unit": unit}
    if retry:
        args["retry"] = list(retry)
    return Command("RefactorUnit", "martin", args, stream(unit))


@refactor.policy("UnitReviewed", "RefactorCommitted")
def next_refactor(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    if parked(ctx, unit):
        return []
    items = _items_left(ctx, unit)
    if items:
        return [_refactor_turn(unit, items[0], None)]
    return [Command("CheckUnit", "beck", {"unit": unit}, event.stream)]


@refactor.translator("RefactorUnit")
def refactor_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "unit": unit, **output["change"]}, actor="martin")]
    return [Result("RefactorDeclared", {"unit": unit, **output["result"]}, actor="martin")]


@refactor.translator("FixStaticFindings")
def static_fixes_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    if command.payload["args"].get("branch"):  # the checks of the whole project, on the feature branch
        return [Result("BranchFixesDeclared", {}, actor="martin")]
    unit = command.payload["args"]["unit"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "unit": unit, **output["change"]}, actor="martin")]
    return [Result("RefactorDeclared", {"unit": unit, "phase": "code", "addressed": ["static-checks"]},
                   actor="martin")]


@refactor.policy("RefactorDeclared")
def verify_refactor(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifyRefactor", "beck", {**event.payload, "turn": event.causation_id}, event.stream)]


def _last_measures(ctx: Ctx, unit: str) -> dict[str, Any]:
    events = ctx.store.read(stream=stream(unit), types_=("UnitMeasured", "RefactorVerified"))
    return [e.payload["measures"] for e in events][-1]


@refactor.handler("VerifyRefactor")
def handle_verify_refactor(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    unit, phase = a["unit"], a["phase"]
    tree = ctx.workspace.worktree(unit)
    changed = changed_files(tree)
    tests = [f for f in changed if is_test_file(Path(f))]
    sources = [f for f in changed if f not in tests]

    def rejected(reason: str, problems: list[str], failure: str | None = None) -> list[Result]:
        payload = {"unit": unit, "reason": reason, "signature": signature(problems)}
        if failure:
            payload["failure"] = failure
        return [Result("RefactorRejected", payload)]

    if not changed:
        return rejected("nothing changed", ["nothing"])
    if phase == "tests" and sources:
        return rejected(f"a refactor of the tests changes no source; changed: {', '.join(sources)}", sources)
    if phase == "code" and tests:
        return rejected(f"a refactor of the code changes no test; changed: {', '.join(tests)}", tests)
    run, report = _run_unit(ctx, unit, command)
    if report is None or report.not_passed:
        return rejected("the unit's tests do not pass after the refactor",
                        [c.id for c in report.not_passed] if report else ["no-report"], _failure(run, report))
    after = _measures(ctx, unit)
    worse = worsened(_last_measures(ctx, unit), after)
    if worse and phase != "strengthen":
        return [Result("MeasureWorsened", {"unit": unit, "worsened": worse, "addressed": a["addressed"],
                                           "measures": after})]
    return [Result("RefactorVerified", {"unit": unit, "addressed": a["addressed"], "measures": after})]


def _martin_turn(ctx: Ctx, rejection: Event) -> Event:
    verifier = ctx.store.get(rejection.causation_id or "")
    return ctx.store.get(verifier.payload["args"]["turn"])


@refactor.policy("RefactorRejected", "SignalRejected")
def retry_refactor(event: Event, ctx: Ctx) -> list[Command]:
    if event.type == "SignalRejected":
        turn = ctx.store.get(event.causation_id or "")
        if turn.payload["command"] not in ("RefactorUnit", "FixStaticFindings", "ReviewUnit", "ResolveConflict"):
            return []
    else:
        turn = _martin_turn(ctx, event)
    if turn.payload["args"].get("branch"):
        return []  # the feature branch's static fixes: beck.machine.closing retries them
    unit = turn.payload["args"]["unit"]
    sequence = [e.payload["signature"] for e in ctx.events(stream=stream(unit))
                if e.type in ("RefactorRejected", "ChecksFailed", "ConflictRejected", "LandingConflicted")
                or (e.type == "SignalRejected" and e.payload.get("command") in
                    ("RefactorUnit", "FixStaticFindings", "ReviewUnit", "ResolveConflict"))]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [Command("BlockRefactor", "beck", {"unit": unit, "reason": why}, stream(unit))]
    reason = event.payload.get("reason") or "; ".join(event.payload.get("errors", []))
    if event.payload.get("failure"):
        reason = f"{reason}\n{event.payload['failure']}"
    args = {**turn.payload["args"], "retry": [len(sequence) + 1, reason[:2000]]}
    args.pop("answer", None)
    again = Command(turn.payload["command"], "martin", args, stream(unit))
    if turn.payload["command"] in ("RefactorUnit", "FixStaticFindings"):  # start again from the last commit
        return [Command("RevertRefactor", "beck", {"unit": unit}, stream(unit)), again]
    return [again]


@refactor.handler("RevertRefactor")
def handle_revert(command: Event, ctx: Ctx) -> list[Result]:
    tree = ctx.workspace.worktree(command.payload["args"]["unit"])
    _git(tree, "reset", "-q", "--hard")
    _git(tree, "clean", "-fdq", "--", ".", ":!.beck")
    return []


@refactor.handler("BlockRefactor")
def handle_block_refactor(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("RefactorBlocked", {"unit": a["unit"], "reason": a["reason"]})]


@refactor.policy("RefactorBlocked")
def park_for_a_blocked_refactor(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return _park_and_propose(ctx, event, [p["unit"]], "blocked_cycle", dict(p),
                             f"martin's work on {p['unit']} blocked: {p['reason']}")


# --- a worsened measure: the user decides --------------------------------------------------------


@refactor.policy("MeasureWorsened")
def ask_tradeoff(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [Command("ParkUnits", "beck", {"units": [p["unit"]], "reason": "a refactor worsens " + ", ".join(p["worsened"]),
                                          "because_of": event.id}, event.stream),
            Command("ApproveTradeoff", "user", {"unit": p["unit"], "worsened": p["worsened"],
                                                "addressed": p["addressed"], "measures": p["measures"]},
                    event.stream)]


@refactor.policy("TradeoffAccepted", "TradeoffRejected")
def after_tradeoff(event: Event, ctx: Ctx) -> list[Command]:
    ask = ctx.store.get(event.causation_id or "").payload["args"]
    unit = ask["unit"]
    if event.type == "TradeoffAccepted":
        return [Command("ResumeAfterTradeoff", "beck", {"unit": unit, "commit": True, "addressed": ask["addressed"],
                                                        "measures": ask["measures"]}, stream(unit))]
    reason = "the user rejected the trade-off (" + ", ".join(ask["worsened"]) + ")"
    if event.payload.get("instructions"):
        reason += ": " + event.payload["instructions"]
    items = _items_left(ctx, unit)
    item = next((i for i in items if [x.get("id") or x.get("test") for x in next(iter(i.values()))]
                 == ask["addressed"]), items[0] if items else None)
    commands = [Command("RevertRefactor", "beck", {"unit": unit}, stream(unit)),
                Command("ResumeAfterTradeoff", "beck", {"unit": unit, "commit": False}, stream(unit))]
    if item is None:  # static fixes: the checks run again
        return commands + [Command("CheckUnit", "beck", {"unit": unit}, stream(unit))]
    return commands + [_refactor_turn(unit, item, (2, reason))]


@refactor.handler("ResumeAfterTradeoff")
def handle_resume_after_tradeoff(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    results = [Result("UnitResumed", {"unit": a["unit"], "base": _base(ctx, a["unit"])})]
    if a["commit"]:
        results.append(Result("RefactorVerified", {"unit": a["unit"], "addressed": a["addressed"],
                                                   "measures": a["measures"]}))
    return results


@refactor.policy("RefactorVerified")
def commit_refactor(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitRefactor", "beck", dict(event.payload), event.stream)]


@refactor.handler("CommitRefactor")
def handle_commit_refactor(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    tree = ctx.workspace.worktree(a["unit"])
    sha = commit(tree, f"fixup! {_subject(ctx, a['unit'])}")
    return [Result("RefactorCommitted", {"unit": a["unit"], "sha": sha, "addressed": a["addressed"]})]


# --- the static checks, then the autosquash ------------------------------------------------------


@refactor.handler("CheckUnit")
def handle_check_unit(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    toolchain = load(ctx.project)
    paths = [p for p in ctx.workspace.scope(unit) if (tree / p).exists()]
    if not toolchain.check or not paths:
        return [Result("ChecksPassed", {"unit": unit})]
    result = static_checks.run(tree, toolchain, paths, whole_project=False, actor="beck", unit=unit,
                               store_root=ctx.project, cause_id=command.id, iteration=ctx.iteration)
    if result.exit_code == 0:
        return [Result("ChecksPassed", {"unit": unit})]
    findings = [str(f) for r in result.results if r.findings for f in r.findings] or [result.summary]
    return [Result("ChecksFailed", {"unit": unit, "findings": findings, "signature": signature(findings)})]


@refactor.policy("ChecksFailed")
def fix_static(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    sequence = [e.payload["signature"] for e in ctx.events(stream=stream(unit)) if e.type == "ChecksFailed"]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [Command("BlockRefactor", "beck", {"unit": unit, "reason": f"static checks: {why}"}, stream(unit))]
    context = {"findings": event.payload["findings"], "check": f"beck check --unit {unit}"}
    return [Command("FixStaticFindings", "martin", {"session": f"martin/{unit}", "context": context, "unit": unit},
                    event.stream)]


@refactor.policy("ChecksPassed")
def autosquash(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("AutosquashUnit", "beck", {"unit": event.payload["unit"]}, event.stream)]


def autosquash_onto(tree: Path, base: str) -> None:
    done = _git(tree, "rebase", "-q", "-i", "--autosquash", "--autostash", base, check=False,
                env={"GIT_SEQUENCE_EDITOR": ":", "GIT_EDITOR": "true"})
    if done.returncode != 0:
        _git(tree, "rebase", "--abort", check=False)
        raise RuntimeError(f"the autosquash onto {base[:8]} failed in {tree}: {done.stderr.strip()}")


@refactor.handler("AutosquashUnit")
def handle_autosquash(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    autosquash_onto(tree, _base(ctx, unit))
    return [Result("UnitReady", {"unit": unit, "sha": _head(tree)})]


# --- the landing -------------------------------------------------------------------------------------


def _landed(ctx: Ctx) -> set[str]:
    return {e.payload["unit"] for e in ctx.events(types=("UnitLanded",))}


def _ready(ctx: Ctx) -> list[str]:
    """The units ready to land and not landing yet, in the plan's order."""
    ready: dict[str, int] = {}
    landing: set[str] = set()
    for e in ctx.events(types=("UnitReady", "UnitLanded", "LandingConflicted", "LandingDeferred", "CommandIssued",
                               "CommandDone")):
        p = e.payload
        if e.type == "UnitReady":
            ready[p["unit"]] = e.seq
            landing.discard(p["unit"])
        elif e.type == "UnitLanded":
            ready.pop(p["unit"], None)
        elif e.type == "CommandIssued" and p["command"] == "LandUnit":
            landing.add(p["args"]["unit"])
        elif e.type in ("LandingDeferred",):
            landing.discard(p["unit"])
        elif e.type == "LandingConflicted":
            ready.pop(p["unit"], None)
    order = list(plan(ctx))
    return [u for u in order if u in ready and u not in landing]


@refactor.policy("UnitReady", "UnitLanded", "ResolutionApplied")
def land(event: Event, ctx: Ctx) -> list[Command]:
    planned, landed = plan(ctx), _landed(ctx)
    for unit in _ready(ctx):
        if set(planned[unit]["depends_on"]) <= landed:
            return [Command("LandUnit", "beck", {"unit": unit}, stream(unit))]  # one at a time, in order
    return []


@refactor.handler("LandUnit")
def handle_land(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    if _git(ctx.project, "status", "--porcelain", "--", ".", ":!.beck").stdout.strip():
        return [Result("LandingDeferred", {"unit": unit, "reason": "the feature branch has uncommitted changes"})]
    target = _head(ctx.project)
    rebased = _git(tree, "rebase", "-q", target, check=False)
    if rebased.returncode != 0:
        files = _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
        return [Result("LandingConflicted", {"unit": unit, "files": files, "signature": signature(files)})]
    branch = _git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(ctx.project, "merge", "-q", "--ff-only", branch)
    return [Result("UnitLanded", {"unit": unit, "sha": _head(ctx.project)})]


@refactor.policy("LandingConflicted")
def resolve_conflict(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    context = {"files": event.payload["files"], "landed": sorted(_landed(ctx)), "run": f"beck test --unit {unit}"}
    return [Command("ResolveConflict", "martin", {"session": f"martin/{unit}", "context": context, "unit": unit},
                    event.stream)]


@refactor.translator("ResolveConflict")
def conflict_resolved(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "unit": unit, **output["change"]}, actor="martin")]
    return [Result("ConflictResolved", {"unit": unit}, actor="martin")]


@refactor.policy("ConflictResolved")
def continue_landing(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ContinueLanding", "beck", {"unit": event.payload["unit"], "turn": event.causation_id},
                    event.stream)]


@refactor.handler("ContinueLanding")
def handle_continue(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    rebase_dir = Path(_git(tree, "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge").stdout.strip())
    if rebase_dir.exists():
        _git(tree, "add", "-A", "--", ".", ":!.beck")
        done = _git(tree, "rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
        if done.returncode != 0:
            files = _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
            return [Result("LandingConflicted", {"unit": unit, "files": files, "signature": signature(files)})]
    run, report = _run_unit(ctx, unit, command)
    if report is None or report.not_passed:
        problems = [c.id for c in report.not_passed] if report else ["no-report"]
        return [Result("ConflictRejected", {"unit": unit, "reason": "the unit's tests fail after the resolution",
                                            "signature": signature(problems), "failure": _failure(run, report)})]
    _git(tree, "add", "-A", "--", ".", ":!.beck")
    if _git(tree, "diff", "--cached", "--quiet", check=False).returncode != 0:  # fixes after the rebase
        _git(tree, "commit", "-q", "--amend", "--no-edit")
    return [Result("UnitReady", {"unit": unit, "sha": _head(tree)})]


@refactor.policy("ConflictRejected")
def retry_conflict(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    sequence = [e.payload["signature"] for e in ctx.events(stream=stream(unit))
                if e.type in ("ConflictRejected", "LandingConflicted")]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [Command("BlockRefactor", "beck", {"unit": unit, "reason": f"landing: {why}"}, stream(unit))]
    turn = ctx.store.get(ctx.store.get(event.causation_id or "").payload["args"]["turn"])
    args = {**turn.payload["args"],
            "retry": [len(sequence) + 1, f"{event.payload['reason']}\n{event.payload.get('failure', '')}"[:2000]]}
    return [Command("ResolveConflict", "martin", args, stream(unit))]


@refactor.policy("UnitLanded")
def close_unit(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CloseUnit", "beck", {"unit": event.payload["unit"]}, event.stream)]


@refactor.handler("CloseUnit")
def handle_close(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    if tree.exists():
        branch = _git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        _git(ctx.project, "worktree", "remove", "--force", str(tree))
        _git(ctx.project, "branch", "-q", "-D", branch, check=False)
    return [Result("UnitClosed", {"unit": unit})]


units_machine = implementation.merge(refactor)
