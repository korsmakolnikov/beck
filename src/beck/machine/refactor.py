"""The machine of phase 5: martin's refactor of a component, its squash, its landing (README, phase 5).

    ComponentImplemented → RunComponentTests (beck) → CoverageMeasured (and CoverageDecreased, reported at the review)
      → ComponentVerified | RegressionFound (→ CycleReopened, green)
      | CoverageGapsFound [first time] → CoverLines (unclebob) → CoverageTestsDeclared → VerifyCover
        → CoverageTestsVerified → CommitCoverage → CoverageTestsCommitted → RunComponentTests
        | CoverageRejected ⟲ CoverLines, once | GiveUpCoverage → RunComponentTests (the gaps go to martin)
      | CoverageDecreased → Ask (the user, routed by kent): go-on → ComponentVerified | park-component | cover-again
    ComponentVerified [not squashed] → SquashComponent → ComponentSquashed → MeasureComponent → ComponentMeasured →
    ReviewComponent (martin) → ComponentReviewed
    ComponentReviewed | RefactorCommitted [items left] → RefactorComponent (martin: one weak test, or one finding)
      → RefactorDeclared → VerifyRefactor (beck runs the component's tests, checks the phase and the measures)
      → RefactorVerified → CommitRefactor (a fixup of the component's commit) → RefactorCommitted
      | RefactorRejected ⟲ RefactorComponent | MeasureWorsened → parked, ApproveTradeoff (the user)
    [no item left] → CheckComponent (beck) → ChecksPassed | ChecksFailed → FixStaticFindings (martin) → RefactorDeclared …
    ChecksPassed → AutosquashComponent → ComponentReady
    ComponentReady | ComponentLanded | RestartApplied → LandComponent [its dependencies landed]
      → ComponentLanded | LandingDeferred | LandingConflicted → ResolveConflict (martin) → ConflictResolved → ContinueLanding
    ComponentLanded → CloseComponent → ComponentClosed
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
from beck.machine.component import _park_and_propose, execution, implementation, stream
from beck.machine.core import Command, Ctx, Issued, Machine, Result, action, ask_routed, request_of
from beck.machine.cycle import commit, parked
from beck.machine.cycle import stream as cycle_stream
from beck.machine.signatures import blocked, signature
from beck.machine.verify import changed_files
from beck.measures import coverage_gaps, files_in, measure, ratchet, totals, worsened
from beck.measures.coverage import READERS, FileCoverage
from beck.testrun import run as run_tests
from beck.toolchain import load
from beck.unimplemented import unimplemented

refactor = Machine()


def _git(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})})


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _base(ctx: Ctx, component: str) -> str:
    """The commit the component's work starts from: its ComponentStarted's, or the last rebase's."""
    events = ctx.store.read(stream=stream(component), types_=("ComponentStarted", "ComponentResumed", "ComponentReopened"))
    return [e.payload["base"] for e in events][-1]


def _subject(ctx: Ctx, component: str) -> str | None:
    squashed = list(ctx.store.read(stream=stream(component), types_=("ComponentSquashed",)))
    return squashed[-1].payload["subject"] if squashed else None


def _declared_tests(ctx: Ctx, component: str) -> dict[str, str]:
    """The component's tests, as the cycles and the coverage declared them: test → requirement."""
    tests: dict[str, str] = {}
    for e in ctx.store.read(types_=("RedVerified", "CoverageTestsVerified")):
        if e.payload["component"] != component:
            continue
        if e.type == "RedVerified":
            tests[e.payload["test"]] = e.payload["requirement"]
        else:
            tests.update({t["test"]: t["requirement"] for t in e.payload["tests"]})
    return tests


def _fitness(tree: Path) -> list[str]:
    return [d for d in ("tests/fitness", "test/fitness") if (tree / d).is_dir()]


def _run_component(ctx: Ctx, component: str, command: Issued, extra: list[str] | None = None) -> tuple[Event, Any]:
    """beck runs the component's tests and the fitness functions, asked by a policy; returns the run and its report."""
    tree = ctx.workspace.worktree(component)
    scheduled = {c for g in execution(ctx)[component]["units"] for c in g["requirements"]}
    declared = [t for t, c in _declared_tests(ctx, component).items() if c in scheduled]   # a dropped requirement's test is gone
    toolchain = load(ctx.project)
    selectors = (list(dict.fromkeys([*declared, *(extra or [])])) + _fitness(tree)
                 + toolchain.doctest_paths(tree, ctx.workspace.scope(component)))
    result = run_tests(tree, toolchain, selectors, actor="beck", component=component, store_root=ctx.project,
                       store=ctx.store, cause=command.cause, coverage_of=ctx.workspace.scope(component))
    report = None
    if "report" in result.recorded.payload:
        try:
            report = parse(ctx.project / result.recorded.payload["report"], tree)
        except UnreadableReport:
            report = None
    return result.recorded, report


def _failure(run: Event, report: Any) -> str:
    if report is None:
        return f"NO_REPORT (exit {run.payload['exit_code']}): see {run.payload['log']}"
    return summary(report)


# --- the component's tests, then the squash --------------------------------------------------------------


@refactor.policy("ComponentImplemented")
def run_component_tests(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunComponentTests", "beck", {"component": event.payload["component"]}, stream(event.payload["component"]))]


@refactor.handler("RunComponentTests")
def handle_run_component_tests(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    run, report = _run_component(ctx, component, command)
    if report is None or report.not_passed:
        failing = [c.id for c in report.not_passed] if report else []
        return [Result("RegressionFound", {"component": component, "tests": failing, "failure": _failure(run, report)})]
    left = _unimplemented_left(ctx, component)
    if left:
        return left
    coverage = _coverage_of(ctx, run, ctx.workspace.worktree(component))
    if coverage is None:
        return [Result("ComponentVerified", {"component": component})]
    return _judge_coverage(ctx, component, coverage)


@refactor.policy("RegressionFound")
def reopen_regressions(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [Command("ReopenRegressions", "beck", dict(p), event.stream)]


def _unimplemented_requirements(ctx: Ctx, component: str, left: list[tuple[str, int]]) -> list[str]:
    """The scheduled requirements of the component that reference these unimplemented: a requirement directly, a morphism by its
    first requirement."""
    tree = ctx.workspace.worktree(component)
    units = execution(ctx)[component]["units"]
    scheduled = {c for g in units for c in g["requirements"]}
    of_morphism = {g["id"].split(":", 1)[1]: g["requirements"] for g in units if g["id"].startswith("morphism:")}
    found: list[str] = []
    for file in dict.fromkeys(f for f, _ in left):
        for ids in ids_at(tree, Path(file), {n for f, n in left if f == file}).values():
            for i in ids:
                found += [i] if i in scheduled else of_morphism.get(i, [])[:1]
    return list(dict.fromkeys(found))


def _doctest_requirements(ctx: Ctx, component: str, failing: list[str]) -> list[str]:
    """The scheduled requirements a failing doctest of the component's sources hands back to unclebob: those that reference the
    unimplemented left in its file (an example kent wrote, on a body no green has written yet)."""
    files = {t.split("::")[0] for t in failing if "::" in t and not is_test_file(Path(t.split("::")[0]))}
    if not files:
        return []
    tree = ctx.workspace.worktree(component)
    return _unimplemented_requirements(ctx, component, [(f, n) for f, n in unimplemented(tree, load(ctx.project)) if f in files])


def _unimplemented_left(ctx: Ctx, component: str) -> list[Result]:
    """When the cycles end, an unimplemented still in the component's scope goes back to the requirement that references it, at
    green: no
    test needed it, so no green wrote it. Past the repetition rule's times, or with no requirement, to a restart."""
    tree = ctx.workspace.worktree(component)
    scope = ctx.workspace.scope(component)
    left = [(f, n) for f, n in unimplemented(tree, load(ctx.project))
            if any(f == p or f.startswith(p.rstrip("/") + "/") for p in scope)]
    if not left:
        return []
    where = ", ".join(f"{f}:{n}" for f, n in left)
    tries = [e for e in ctx.store.read(types_=("CycleReopened",))
             if e.payload["component"] == component and e.payload["reason"].startswith("an unimplemented left")]
    requirements = _unimplemented_requirements(ctx, component, left) if len(tries) < ctx.limits[0] else []
    if not requirements:
        return [Result("RegressionUnassigned", {"component": component,
                                                "failure": f"unimplemented no green implemented: {where}"})]
    units = {e.payload["requirement"]: e.payload["unit"] for e in ctx.store.read(types_=("CycleOpened",))
              if e.payload["component"] == component}
    return [Result("CycleReopened", {"component": component, "requirement": c, "unit": units.get(c, "component"),
                                     "phase": "green",
                                     "reason": f"an unimplemented left: {where}; write it"}, stream=cycle_stream(component, c))
            for c in requirements]


@refactor.handler("ReopenRegressions")
def handle_reopen(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    component, tests = a["component"], _declared_tests(ctx, a["component"])
    units = {e.payload["requirement"]: e.payload["unit"] for e in ctx.store.read(types_=("CycleOpened",))
              if e.payload["component"] == component}
    scheduled = {c for g in execution(ctx)[component]["units"] for c in g["requirements"]}
    requirements = sorted({tests[t] for t in a["tests"] if t in tests and tests[t] in scheduled})  # no dropped requirement
    requirements = sorted({*requirements, *_doctest_requirements(ctx, component, [t for t in a["tests"] if t not in tests])})
    if not requirements:  # a fitness function, or no report: nothing to hand to unclebob
        return [Result("RegressionUnassigned", {"component": component, "failure": a["failure"][:2000]})]
    return [Result("CycleReopened", {"component": component, "requirement": c, "unit": units[c], "phase": "green",
                                     "reason": f"a regression: {a['failure'][:500]}"}, stream=cycle_stream(component, c))
            for c in requirements]


@refactor.policy("RegressionUnassigned")
def resolve_unassigned(event: Event, ctx: Ctx) -> list[Command]:
    """A failing test no scheduled cycle owns: the component parks with a restart to approve, never with no way out."""
    p = event.payload
    reason = f"the component's tests fail outside its cycles: {p['failure'][:300]}"
    return _park_and_propose(ctx, event, [p["component"]], "blocked_cycle", {"component": p["component"], "reason": reason},
                             reason)


# --- the coverage: measured, its gaps tried once, never a stop -----------------------------------


def _coverage_of(ctx: Ctx, run: Event, tree: Path) -> dict[str, FileCoverage] | None:
    if "coverage" not in run.payload:
        return None
    try:
        return READERS[run.payload.get("coverage_format", "coverage.py")](ctx.project / run.payload["coverage"], tree)
    except (OSError, ValueError, KeyError):
        return None


def _baseline(ctx: Ctx) -> dict[str, list[int]]:
    """The project's coverage: the start's suite, then every landed component's files as they landed."""
    baseline: dict[str, list[int]] = {}
    last: dict[str, dict[str, list[int]]] = {}
    for e in ctx.events(types=("CoverageMeasured", "ComponentLanded")):
        if e.type == "CoverageMeasured" and "component" not in e.payload:
            baseline = dict(e.payload["files"])
        elif e.type == "CoverageMeasured":
            last[e.payload["component"]] = e.payload["files"]
        elif e.payload["component"] in last:
            baseline |= last[e.payload["component"]]
    return baseline


def _judge_coverage(ctx: Ctx, component: str, coverage: dict[str, FileCoverage]) -> list[Result]:
    tree = ctx.workspace.worktree(component)
    files = files_in(tree, ctx.workspace.scope(component))
    sources = [f for f in files if not is_test_file(Path(f))]
    gaps = coverage_gaps(coverage, tree, _base(ctx, component), sources)
    measured = totals(coverage, sources)
    before, after = ratchet(_baseline(ctx), measured, sources)
    results = [Result("CoverageMeasured", {"component": component, "files": measured, "gaps": gaps, "before": before,
                                           "after": after})]
    tried = any(e.payload["command"] == "CoverLines"
                for e in ctx.events(stream=stream(component), types=("AgentTurnRequested",)))
    requirements = _requirements_of_gaps(ctx, component, tree, gaps) if gaps else {}
    if requirements and not tried:
        return results + [Result("CoverageGapsFound", {"component": component, "requirements": requirements})]
    if before is not None and after is not None and after < before:   # TDD never lowers it: the user decides
        return results + [Result("CoverageDecreased", {"component": component, "before": before, "after": after,
                                                       "requirements": requirements})]
    return results + [Result("ComponentVerified", {"component": component})]


@refactor.policy("CoverageDecreased")
def ask_about_the_coverage(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    actions = [action("go-on", "Accept the lower coverage: the component goes on to its review."),
               action("park-component", "Park the component; the other components go on.")]
    if p.get("requirements"):
        actions.insert(0, action("cover-again", "Ask unclebob again for the tests that run the lines, "
                                                "with the user's words."))
    measured = [e for e in ctx.events(stream=stream(p["component"]), types=("CoverageMeasured",))][-1]
    return [ask_routed(f"{p['component']} lowers the project's coverage from {p['before']} % to {p['after']} %; "
                       f"lines no test runs: {measured.payload['gaps']}",
                       "With TDD the coverage never goes down.", "coverage-decreased", actions, event.stream,
                       event.id)]


@refactor.policy("DecisionRouted")
def after_the_coverage_question(event: Event, ctx: Ctx) -> list[Command]:
    if event.payload["situation"] != "coverage-decreased":
        return []
    decreased = ctx.store.get(ctx.store.get(event.payload["ask"]).payload["args"]["turn"])
    component, chosen = decreased.payload["component"], event.payload["action"]
    if chosen == "go-on":
        return [Command("AcceptCoverage", "beck", {"component": component}, stream(component))]
    if chosen == "park-component":
        return [Command("ParkComponents", "beck", {"components": [component], "reason": f"the coverage: {event.payload['why']}",
                                              "because_of": event.id}, stream(component))]
    return [_cover_turn(ctx, component, decreased.payload["requirements"], (2, f"the user: {event.payload['why']}"))]


@refactor.handler("AcceptCoverage")
def handle_accept_coverage(command: Issued, ctx: Ctx) -> list[Result]:
    return [Result("ComponentVerified", {"component": command.payload["args"]["component"]}, actor="user")]


def _requirements_of_gaps(ctx: Ctx, component: str, tree: Path, gaps: dict[str, list[int]]) -> dict[str, dict[str, list[int]]]:
    """{requirement: {file: lines}}: each line to the requirement that names its code, through the `spec:` markers."""
    units: dict[str, list[str]] = {}
    for e in ctx.store.read(types_=("CycleOpened",)):
        if e.payload["component"] == component and e.payload["requirement"] not in units.setdefault(e.payload["unit"], []):
            units[e.payload["unit"]].append(e.payload["requirement"])
    requirements = {c for cs in units.values() for c in cs}
    out: dict[str, dict[str, list[int]]] = {}
    for f, lines in gaps.items():
        for line, ids in sorted(ids_at(tree, Path(f), set(lines)).items()):
            requirement = next((i for i in ids if i in requirements), None)
            if requirement is None:
                requirement = next((units[g][0] for i in ids for g in (f"morphism:{i}", f"model:{i}") if g in units), None)
            if requirement is not None:
                out.setdefault(requirement, {}).setdefault(f, []).append(line)
    return out


def _cover_turn(ctx: Ctx, component: str, requirements: dict[str, dict[str, list[int]]], retry: tuple[int, str] | None
                ) -> Command:
    tests: dict[str, list[str]] = {}
    for test, requirement in _declared_tests(ctx, component).items():
        tests.setdefault(requirement, []).append(test)
    context = {"requirements": [{"id": c, "text": ctx.workspace.requirement_text(c), "tests": tests.get(c, []), "lines": lines}
                           for c, lines in requirements.items()],
               "run": f"beck send test <the tests> --component {component}"}
    args: dict[str, Any] = {"session": f"{component}/coverage", "context": context, "component": component,
                            "requirements": requirements}
    if retry:
        args["retry"] = list(retry)
    return Command("CoverLines", "unclebob", args, stream(component))


@refactor.policy("CoverageGapsFound")
def cover_lines(event: Event, ctx: Ctx) -> list[Command]:
    return [_cover_turn(ctx, event.payload["component"], event.payload["requirements"], None)]


@refactor.translator("CoverLines")
def coverage_tests_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    if output["outcome"] == "change_requested":   # covering is optional: a change is not worth stopping for
        return [Result("CoverageRejected", {"component": component, "reason": "unclebob asked for a change: "
                                            + output["change"]["problem"], "signature": signature(["change"])},
                       actor="unclebob")]
    return [Result("CoverageTestsDeclared", {"component": component, "tests": output["result"]["tests"]}, actor="unclebob")]


@refactor.policy("CoverageTestsDeclared")
def verify_cover(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifyCover", "beck", {**event.payload, "turn": event.causation_id}, event.stream)]


@refactor.handler("VerifyCover")
def handle_verify_cover(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    component, declared = a["component"], a["tests"]
    tree = ctx.workspace.worktree(component)
    turn = ctx.store.get(a["turn"])
    requirements = turn.payload["args"]["requirements"]
    changed = changed_files(tree)
    sources = [f for f in changed if not is_test_file(Path(f))]

    def rejected(reason: str, problems: list[str], failure: str | None = None) -> list[Result]:
        payload = {"component": component, "reason": reason, "signature": signature(problems)}
        if failure:
            payload["failure"] = failure
        return [Result("CoverageRejected", payload)]

    if sources:
        return rejected(f"covering changes no source; changed: {', '.join(sources)}", sources)
    if not changed:
        return rejected("no test was written", ["nothing"])
    unknown = [t["requirement"] for t in declared if t["requirement"] not in requirements]
    if unknown:
        return rejected(f"the tests reference requirements outside the request: {', '.join(unknown)}", unknown)
    run, report = _run_component(ctx, component, command, [t["test"] for t in declared])
    if report is None or report.not_passed:
        return rejected("the component's tests do not pass with the new tests",
                        [c.id for c in report.not_passed] if report else ["no-report"], _failure(run, report))
    ran = {c.id for c in report.cases}
    missing = [t["test"] for t in declared if t["test"] not in ran]
    if missing:
        return rejected(f"the declared tests did not run: {', '.join(missing)}", missing)
    coverage = _coverage_of(ctx, run, tree)
    if coverage is not None:
        wanted = {(f, line) for lines in requirements.values() for f, ls in lines.items() for line in ls}
        still = {(f, line) for f, line in wanted if f not in coverage or line in coverage[f].missing}
        if still == wanted:
            return rejected("the new tests run none of the lines", sorted(f"{f}:{n}" for f, n in still))
    return [Result("CoverageTestsVerified", {"component": component, "tests": declared})]


@refactor.policy("CoverageTestsVerified")
def commit_cover(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitCoverage", "beck", {"component": event.payload["component"],
                                               "requirements": sorted({t["requirement"] for t in event.payload["tests"]})},
                    event.stream)]


@refactor.handler("CommitCoverage")
def handle_commit_cover(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    sha = commit(ctx.workspace.worktree(a["component"]), f"test({a['component']}): cover {', '.join(a['requirements'])}")
    return [Result("CoverageTestsCommitted", {"component": a["component"], "sha": sha})]


@refactor.policy("CoverageTestsCommitted")
def run_again_after_cover(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunComponentTests", "beck", {"component": event.payload["component"]}, event.stream)]


@refactor.policy("CoverageRejected", "SignalRejected")
def retry_cover(event: Event, ctx: Ctx) -> list[Command]:
    turn = request_of(ctx.store, event)   # the CoverLines turn: rejected itself, or its verification
    if turn.payload["command"] != "CoverLines":
        return []
    component = turn.payload["args"]["component"]
    attempts = [e for e in ctx.events(stream=stream(component), types=("AgentTurnRequested",))
                if e.payload["command"] == "CoverLines"]
    revert = Command("RevertRefactor", "beck", {"component": component}, stream(component))
    reason = event.payload.get("reason") or "; ".join(event.payload.get("errors", []))
    if len(attempts) >= 2:
        return [revert, Command("GiveUpCoverage", "beck", {"component": component, "reason": reason[:500]}, stream(component))]
    if event.payload.get("failure"):
        reason = f"{reason}\n{event.payload['failure']}"
    return [revert, _cover_turn(ctx, component, turn.payload["args"]["requirements"], (2, reason[:2000]))]


@refactor.handler("GiveUpCoverage")
def handle_give_up_coverage(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("CoverageGivenUp", {"component": a["component"], "reason": a["reason"]})]


@refactor.policy("CoverageGivenUp")
def judge_again_after_giving_up(event: Event, ctx: Ctx) -> list[Command]:
    """The tests run again without the try: the coverage is judged as it is (no second try)."""
    return [Command("RunComponentTests", "beck", {"component": event.payload["component"]}, event.stream)]


@refactor.policy("ComponentVerified")
def squash_or_measure(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    name = "MeasureComponent" if _squashed_as_of(ctx, component) else "SquashComponent"
    return [Command(name, "beck", {"component": component}, event.stream)]


def _squashed_as_of(ctx: Ctx, component: str) -> bool:
    return bool(ctx.events(stream=stream(component), types=("ComponentSquashed",)))


@refactor.handler("SquashComponent")
def handle_squash(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    requirements = list(dict.fromkeys(_declared_tests(ctx, component).values()))
    subject = f"feat({component}): implement {component}"
    if _subject(ctx, component) is None and _git(tree, "log", "-1", "--format=%s").stdout.strip() != subject:
        _git(tree, "reset", "-q", "--soft", _base(ctx, component))
        # a component with no commit of its own (it implements no requirement) still gets its commit: fixups and landing aim at it
        _git(tree, "commit", "-q", "--allow-empty", "-m", subject,
             *(["-m", "Requirements: " + ", ".join(requirements)] if requirements else []))
    return [Result("ComponentSquashed", {"component": component, "sha": _head(tree), "subject": subject})]


@refactor.policy("ComponentSquashed")
def measure_after_squash(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("MeasureComponent", "beck", {"component": event.payload["component"]}, event.stream)]


def _measures(ctx: Ctx, component: str) -> dict[str, Any]:
    tree = ctx.workspace.worktree(component)
    toolchain = load(ctx.project)
    return measure(tree, files_in(tree, ctx.workspace.scope(component)), toolchain.complexity, toolchain)


@refactor.handler("MeasureComponent")
def handle_measure(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    return [Result("ComponentMeasured", {"component": component, "measures": _measures(ctx, component)})]


# --- martin's review and refactors ------------------------------------------------------------------


@refactor.policy("ComponentMeasured")
def review(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    tree = ctx.workspace.worktree(component)
    tests: dict[str, list[str]] = {}
    for test, requirement in _declared_tests(ctx, component).items():
        tests.setdefault(requirement, []).append(test)
    requirements = [{"id": c, "text": ctx.workspace.requirement_text(c), "tests": ts} for c, ts in tests.items()]
    context = {"component": {"id": component, "namespace": [], "files": files_in(tree, ctx.workspace.scope(component))},
               "measures": event.payload["measures"], "requirements": requirements}
    measured = [e for e in ctx.events(stream=stream(component), types=("CoverageMeasured",))]
    if measured and measured[-1].payload["gaps"]:
        context["uncovered"] = measured[-1].payload["gaps"]
    flaky = [{"test": e.payload["test"], "evidence": e.payload["evidence"]}
             for e in ctx.events(types=("FlakyTestFound",)) if e.payload["test"] in _declared_tests(ctx, component)]
    if flaky:
        context["flaky"] = flaky
    return [Command("ReviewComponent", "martin", {"session": f"martin/{component}", "context": context, "component": component},
                    event.stream)]


@refactor.translator("ReviewComponent")
def component_reviewed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "component": component, **output["change"]}, actor="martin")]
    return [Result("ComponentReviewed", {"component": component, **output["result"]}, actor="martin")]


def _items_left(ctx: Ctx, component: str) -> list[dict[str, Any]]:
    """The weak tests, then the findings, of the last review that no refactor addressed yet."""
    events = ctx.events(stream=stream(component))
    reviews = [e for e in events if e.type == "ComponentReviewed"]
    if not reviews:
        return []
    last = reviews[-1]
    done = {a for e in events if e.type == "RefactorCommitted" and e.seq > last.seq for a in e.payload["addressed"]}
    items = [{"weak_tests": [w]} for w in last.payload["weak_tests"] if w["test"] not in done]
    return items + [{"findings": [f]} for f in last.payload["findings"] if f["id"] not in done]


def _refactor_turn(component: str, item: dict[str, Any], retry: tuple[int, str] | None) -> Command:
    context = {**item, "run": f"beck send test --component {component}", "check": f"beck send check --component {component}"}
    args: dict[str, Any] = {"session": f"martin/{component}", "context": context, "component": component}
    if retry:
        args["retry"] = list(retry)
    return Command("RefactorComponent", "martin", args, stream(component))


@refactor.policy("ComponentReviewed", "RefactorCommitted")
def next_refactor(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    if parked(ctx, component):
        return []
    items = _items_left(ctx, component)
    if items:
        return [_refactor_turn(component, items[0], None)]
    return [Command("CheckComponent", "beck", {"component": component}, event.stream)]


@refactor.translator("RefactorComponent")
def refactor_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "component": component, **output["change"]}, actor="martin")]
    return [Result("RefactorDeclared", {"component": component, **output["result"]}, actor="martin")]


@refactor.translator("FixStaticFindings")
def static_fixes_declared(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    if command.payload["args"].get("branch"):  # the checks of the whole project, on the feature branch
        return [Result("BranchFixesDeclared", {}, actor="martin")]
    component = command.payload["args"]["component"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "component": component, **output["change"]}, actor="martin")]
    return [Result("RefactorDeclared", {"component": component, "phase": "code", "addressed": ["static-checks"]},
                   actor="martin")]


@refactor.policy("RefactorDeclared")
def verify_refactor(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifyRefactor", "beck", {**event.payload, "turn": event.causation_id}, event.stream)]


def _last_measures(ctx: Ctx, component: str) -> dict[str, Any]:
    events = ctx.store.read(stream=stream(component), types_=("ComponentMeasured", "RefactorVerified"))
    return [e.payload["measures"] for e in events][-1]


@refactor.handler("VerifyRefactor")
def handle_verify_refactor(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    component, phase = a["component"], a["phase"]
    tree = ctx.workspace.worktree(component)
    changed = changed_files(tree)
    tests = [f for f in changed if is_test_file(Path(f))]
    sources = [f for f in changed if f not in tests]

    def rejected(reason: str, problems: list[str], failure: str | None = None) -> list[Result]:
        payload = {"component": component, "reason": reason, "signature": signature(problems)}
        if failure:
            payload["failure"] = failure
        return [Result("RefactorRejected", payload)]

    if phase == "none":   # the finding is already met at HEAD: nothing to commit, the next item
        if changed:
            return rejected(f"phase none changes nothing; changed: {', '.join(changed)}", changed)
        return [Result("RefactorCommitted", {"component": component, "sha": _git(tree, "rev-parse", "HEAD").stdout.strip(),
                                             "addressed": a["addressed"]})]
    if not changed:
        return rejected("nothing changed", ["nothing"])
    if phase == "tests" and sources:
        return rejected(f"a refactor of the tests changes no source; changed: {', '.join(sources)}", sources)
    if phase == "code" and tests:
        return rejected(f"a refactor of the code changes no test; changed: {', '.join(tests)}", tests)
    run, report = _run_component(ctx, component, command)
    if report is None or report.not_passed:
        return rejected("the component's tests do not pass after the refactor",
                        [c.id for c in report.not_passed] if report else ["no-report"], _failure(run, report))
    after = _measures(ctx, component)
    worse = worsened(_last_measures(ctx, component), after)
    if worse and phase != "strengthen":
        return [Result("MeasureWorsened", {"component": component, "worsened": worse, "addressed": a["addressed"],
                                           "measures": after})]
    return [Result("RefactorVerified", {"component": component, "addressed": a["addressed"], "measures": after})]


def _martin_turn(ctx: Ctx, rejection: Event) -> Event:
    return request_of(ctx.store, rejection)


@refactor.policy("RefactorRejected", "SignalRejected")
def retry_refactor(event: Event, ctx: Ctx) -> list[Command]:
    turn = _martin_turn(ctx, event)
    if turn.payload["command"] not in ("RefactorComponent", "FixStaticFindings", "ReviewComponent", "ResolveConflict"):
        return []
    if turn.payload["args"].get("branch"):
        return []  # the feature branch's static fixes: beck.machine.closing retries them
    component = turn.payload["args"]["component"]
    sequence = [e.payload["signature"] for e in ctx.events(stream=stream(component))
                if e.type in ("RefactorRejected", "ChecksFailed", "ConflictRejected", "LandingConflicted")
                or (e.type == "SignalRejected" and e.payload.get("command") in
                    ("RefactorComponent", "FixStaticFindings", "ReviewComponent", "ResolveConflict"))]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [Command("BlockRefactor", "beck", {"component": component, "reason": why}, stream(component))]
    reason = event.payload.get("reason") or "; ".join(event.payload.get("errors", []))
    if event.payload.get("failure"):
        reason = f"{reason}\n{event.payload['failure']}"
    args = {**turn.payload["args"], "retry": [len(sequence) + 1, reason[:2000]]}
    args.pop("answer", None)
    again = Command(turn.payload["command"], "martin", args, stream(component))
    if turn.payload["command"] in ("RefactorComponent", "FixStaticFindings"):  # start again from the last commit
        return [Command("RevertRefactor", "beck", {"component": component}, stream(component)), again]
    return [again]


@refactor.handler("RevertRefactor")
def handle_revert(command: Issued, ctx: Ctx) -> list[Result]:
    tree = ctx.workspace.worktree(command.payload["args"]["component"])
    _git(tree, "reset", "-q", "--hard")
    _git(tree, "clean", "-fdq", "--", ".", ":!.beck")
    return []


@refactor.handler("BlockRefactor")
def handle_block_refactor(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("RefactorBlocked", {"component": a["component"], "reason": a["reason"]})]


@refactor.policy("RefactorBlocked")
def park_for_a_blocked_refactor(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return _park_and_propose(ctx, event, [p["component"]], "blocked_cycle", dict(p),
                             f"martin's work on {p['component']} blocked: {p['reason']}")


# --- a worsened measure: the user decides --------------------------------------------------------


@refactor.policy("MeasureWorsened")
def ask_tradeoff(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [Command("ParkComponents", "beck", {"components": [p["component"]],
                                               "reason": "a refactor worsens " + ", ".join(p["worsened"]),
                                          "because_of": event.id}, event.stream),
            Command("ApproveTradeoff", "user", {"component": p["component"], "worsened": p["worsened"],
                                                "addressed": p["addressed"], "measures": p["measures"]},
                    event.stream)]


@refactor.policy("TradeoffAccepted", "TradeoffRejected")
def after_tradeoff(event: Event, ctx: Ctx) -> list[Command]:
    ask = ctx.store.get(event.causation_id or "").payload["args"]
    component = ask["component"]
    if event.type == "TradeoffAccepted":
        return [Command("ResumeAfterTradeoff", "beck", {"component": component, "commit": True, "addressed": ask["addressed"],
                                                        "measures": ask["measures"]}, stream(component))]
    reason = "the user rejected the trade-off (" + ", ".join(ask["worsened"]) + ")"
    if event.payload.get("instructions"):
        reason += ": " + event.payload["instructions"]
    items = _items_left(ctx, component)
    item = next((i for i in items if [x.get("id") or x.get("test") for x in next(iter(i.values()))]
                 == ask["addressed"]), items[0] if items else None)
    commands = [Command("RevertRefactor", "beck", {"component": component}, stream(component)),
                Command("ResumeAfterTradeoff", "beck", {"component": component, "commit": False}, stream(component))]
    if item is None:  # static fixes: the checks run again
        return commands + [Command("CheckComponent", "beck", {"component": component}, stream(component))]
    return commands + [_refactor_turn(component, item, (2, reason))]


@refactor.handler("ResumeAfterTradeoff")
def handle_resume_after_tradeoff(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    results = [Result("ComponentResumed", {"component": a["component"], "base": _base(ctx, a["component"])})]
    if a["commit"]:
        results.append(Result("RefactorVerified", {"component": a["component"], "addressed": a["addressed"],
                                                   "measures": a["measures"]}))
    return results


@refactor.policy("RefactorVerified")
def commit_refactor(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitRefactor", "beck", dict(event.payload), event.stream)]


@refactor.handler("CommitRefactor")
def handle_commit_refactor(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    tree = ctx.workspace.worktree(a["component"])
    sha = commit(tree, f"fixup! {_subject(ctx, a['component'])}")
    return [Result("RefactorCommitted", {"component": a["component"], "sha": sha, "addressed": a["addressed"]})]


# --- the static checks, then the autosquash ------------------------------------------------------


@refactor.handler("CheckComponent")
def handle_check_component(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    toolchain = load(ctx.project)
    paths = [p for p in ctx.workspace.scope(component) if (tree / p).exists()]
    if not toolchain.check or not paths:
        return [Result("ChecksPassed", {"component": component})]
    result = static_checks.run(tree, toolchain, paths, whole_project=False, actor="beck", component=component,
                               store_root=ctx.project, store=ctx.store, cause=command.cause)
    if result.exit_code == 0:
        return [Result("ChecksPassed", {"component": component})]
    findings = [str(f) for r in result.results if r.findings for f in r.findings] or [result.summary]
    return [Result("ChecksFailed", {"component": component, "findings": findings, "signature": signature(findings)})]


@refactor.policy("ChecksFailed")
def fix_static(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    sequence = [e.payload["signature"] for e in ctx.events(stream=stream(component)) if e.type == "ChecksFailed"]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [Command("BlockRefactor", "beck", {"component": component, "reason": f"static checks: {why}"}, stream(component))]
    context = {"findings": event.payload["findings"], "check": f"beck send check --component {component}"}
    return [Command("FixStaticFindings", "martin", {"session": f"martin/{component}", "context": context, "component": component},
                    event.stream)]


@refactor.policy("ChecksPassed")
def autosquash(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("AutosquashComponent", "beck", {"component": event.payload["component"]}, event.stream)]


def autosquash_onto(tree: Path, base: str) -> None:
    done = _git(tree, "rebase", "-q", "-i", "--autosquash", "--autostash", base, check=False,
                env={"GIT_SEQUENCE_EDITOR": ":", "GIT_EDITOR": "true"})
    if done.returncode != 0:
        _git(tree, "rebase", "--abort", check=False)
        raise RuntimeError(f"the autosquash onto {base[:8]} failed in {tree}: {done.stderr.strip()}")


@refactor.handler("AutosquashComponent")
def handle_autosquash(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    autosquash_onto(tree, _base(ctx, component))
    return [Result("ComponentReady", {"component": component, "sha": _head(tree)})]


# --- the landing -------------------------------------------------------------------------------------


def _landed(ctx: Ctx) -> set[str]:
    return {e.payload["component"] for e in ctx.events(types=("ComponentLanded",))}


def _ready(ctx: Ctx) -> list[str]:
    """The components ready to land and not landing yet, in the execution's order."""
    ready: dict[str, int] = {}
    for e in ctx.events(types=("ComponentReady", "ComponentLanded", "LandingConflicted")):
        p = e.payload
        if e.type == "ComponentReady":
            ready[p["component"]] = e.seq
        else:   # landed, or landing through its conflict's resolution
            ready.pop(p["component"], None)
    order = list(execution(ctx))
    return [u for u in order if u in ready]


@refactor.policy("ComponentReady", "ComponentLanded", "RestartApplied")
def land(event: Event, ctx: Ctx) -> list[Command]:
    scheduled, landed = execution(ctx), _landed(ctx)
    for component in _ready(ctx):
        if set(scheduled[component]["depends_on"]) <= landed:
            return [Command("LandComponent", "beck", {"component": component}, stream(component))]  # one at a time, in order
    return []


@refactor.handler("LandComponent")
def handle_land(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    if _git(ctx.project, "status", "--porcelain", "--", ".", ":!.beck").stdout.strip():
        return [Result("LandingDeferred", {"component": component, "reason": "the feature branch has uncommitted changes"})]
    target = _head(ctx.project)
    rebased = _git(tree, "rebase", "-q", target, check=False)
    if rebased.returncode != 0:
        files = _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
        return [Result("LandingConflicted", {"component": component, "files": files, "signature": signature(files)})]
    branch = _git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(ctx.project, "merge", "-q", "--ff-only", branch)
    return [Result("ComponentLanded", {"component": component, "sha": _head(ctx.project)})]


@refactor.policy("LandingConflicted")
def resolve_conflict(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    context = {"files": event.payload["files"], "landed": sorted(_landed(ctx)), "run": f"beck send test --component {component}"}
    return [Command("ResolveConflict", "martin", {"session": f"martin/{component}", "context": context, "component": component},
                    event.stream)]


@refactor.translator("ResolveConflict")
def conflict_resolved(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    if output["outcome"] == "change_requested":
        return [Result("ChangeRequested", {"agent": "martin", "component": component, **output["change"]}, actor="martin")]
    return [Result("ConflictResolved", {"component": component}, actor="martin")]


@refactor.policy("ConflictResolved")
def continue_landing(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ContinueLanding", "beck", {"component": event.payload["component"], "turn": event.causation_id},
                    event.stream)]


@refactor.handler("ContinueLanding")
def handle_continue(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    rebase_dir = Path(_git(tree, "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge").stdout.strip())
    if rebase_dir.exists():
        _git(tree, "add", "-A", "--", ".", ":!.beck")
        done = _git(tree, "rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
        if done.returncode != 0:
            files = _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
            return [Result("LandingConflicted", {"component": component, "files": files, "signature": signature(files)})]
    run, report = _run_component(ctx, component, command)
    if report is None or report.not_passed:
        problems = [c.id for c in report.not_passed] if report else ["no-report"]
        return [Result("ConflictRejected", {"component": component, "reason": "the component's tests fail after the restart",
                                            "signature": signature(problems), "failure": _failure(run, report)})]
    _git(tree, "add", "-A", "--", ".", ":!.beck")
    if _git(tree, "diff", "--cached", "--quiet", check=False).returncode != 0:  # fixes after the rebase
        _git(tree, "commit", "-q", "--amend", "--no-edit")
    return [Result("ComponentReady", {"component": component, "sha": _head(tree)})]


@refactor.policy("ConflictRejected")
def retry_conflict(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    sequence = [e.payload["signature"] for e in ctx.events(stream=stream(component))
                if e.type in ("ConflictRejected", "LandingConflicted")]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [Command("BlockRefactor", "beck", {"component": component, "reason": f"landing: {why}"}, stream(component))]
    turn = request_of(ctx.store, event)   # ConflictRejected ← ConflictResolved ← martin's ResolveConflict
    args = {**turn.payload["args"],
            "retry": [len(sequence) + 1, f"{event.payload['reason']}\n{event.payload.get('failure', '')}"[:2000]]}
    return [Command("ResolveConflict", "martin", args, stream(component))]


@refactor.policy("ComponentLanded")
def close_component(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CloseComponent", "beck", {"component": event.payload["component"]}, event.stream)]


@refactor.handler("CloseComponent")
def handle_close(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    if tree.exists():
        branch = _git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        _git(ctx.project, "worktree", "remove", "--force", str(tree))
        _git(ctx.project, "branch", "-q", "-D", branch, check=False)
    return [Result("ComponentClosed", {"component": component})]


components_machine = implementation.merge(refactor)
