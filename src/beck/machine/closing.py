"""The machine of phase 6: integration and closing (README, phase 6).

    UnitClosed [every unit closed] → RunFullSuite (beck, on the feature branch) → SuiteGreen | SuiteFailed
    SuiteFailed → ReopenUnits (each failing test's unit: a new worktree, its cycle reopened at green) ⟲ phases 4 and 5
    SuiteGreen → RunStaticChecks (beck, the whole project) → StaticChecksPassed | StaticChecksFailed
    StaticChecksFailed → FixStaticFindings (martin, on the feature branch) → BranchFixesDeclared
      → CommitBranchFixes (fixups of the commits that wrote the lines, by git blame) ⟲ RunStaticChecks
    StaticChecksPassed [first round] → AutosquashBranch → BranchSquashed → PresentForReview (the user)
    StaticChecksPassed [later round] → PresentForReview (the user's corrections stay as fixups)
    CorrectionsRequested → PlanCorrections (kent) → CorrectionsPlanned → CorrectUnits ⟲ phases 4 and 5
    IterationClosed (the user) → AutosquashBranch, the catalog's end: the pull request is presentable

The units reopened after their landing commit fixups of their own commit;
their autosquash cannot fold them (the target is below the worktree's base),
so they land as fixups and the branch's autosquash folds them, except the
user's corrections, which the next review shows.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from beck import checks as static_checks
from beck.events.project import Project
from beck.events.store import Event
from beck.junit import UnreadableReport, parse, summary
from beck.machine.core import Command, Ctx, Machine, Result, action, ask_routed
from beck.machine.cycle import stream as cycle_stream
from beck.machine.refactor import _declared_tests, _git, _head, autosquash_onto, units_machine
from beck.machine.signatures import blocked, signature
from beck.machine.unit import plan, state, stream
from beck.testrun import run as run_tests
from beck.toolchain import STUBS, load

closing = Machine()
BRANCH = "branch"


def iteration_base(ctx: Ctx) -> str:
    """The feature branch's commit the iteration's units started from."""
    started = list(ctx.store.read(types_=("UnitStarted",)))
    return started[0].payload["base"]


def squash_base(ctx: Ctx) -> str:
    """Where the branch's autosquash starts: before the model's commit, which the units start from, so that its
    fixups (a static finding fixed in an interface) fold into it; the root when the model is the first commit."""
    model = ctx.events(types=("ModelCommitted",))
    if not model:
        return iteration_base(ctx)
    parent = _git(ctx.project, "rev-parse", "--verify", "-q", f"{model[-1].payload['sha']}~1", check=False)
    return parent.stdout.strip() or "--root"


def _all_closed(ctx: Ctx) -> bool:
    planned = plan(ctx)
    return bool(planned) and all(state(ctx, u) == "closed" for u in planned)


@closing.policy("UnitClosed")
def full_suite(event: Event, ctx: Ctx) -> list[Command]:
    if not _all_closed(ctx):
        return []
    return [Command("RunFullSuite", "beck", {}, BRANCH)]


@closing.handler("RunFullSuite")
def handle_full_suite(command: Event, ctx: Ctx) -> list[Result]:
    result = run_tests(ctx.project, load(ctx.project), [], actor="beck", cause_id=command.id,
                       iteration=ctx.iteration)
    report = None
    if "report" in result.event.payload:
        try:
            report = parse(ctx.project / result.event.payload["report"], ctx.project)
        except UnreadableReport:
            pass
    if report is not None and not report.not_passed and report.count("passed"):
        return [Result("SuiteGreen", {})]
    failing = [c.id for c in report.not_passed] if report else []
    failure = summary(report) if report else f"NO_REPORT: see {result.event.payload['log']}"
    return [Result("SuiteFailed", {"tests": failing, "failure": failure[:4000]})]


@closing.policy("SuiteFailed")
def reopen_failing(event: Event, ctx: Ctx) -> list[Command]:
    tests: dict[str, tuple[str, str]] = {}
    for e in ctx.events(types=("RedVerified",)):
        tests[e.payload["test"]] = (e.payload["unit"], e.payload["clause"])
    planned = {(u, c) for u, p in plan(ctx).items() for g in p["groups"] for c in g["clauses"]}
    by_unit: dict[str, list[str]] = {}
    orphans: list[str] = []
    for t in event.payload["tests"]:
        if t in tests and tests[t] in planned:
            unit, clause = tests[t]
            by_unit.setdefault(unit, []).append(clause)
        elif t in tests:   # its clause left the domain: no cycle of it to reopen
            orphans.append(t)
    if not by_unit:  # a test no cycle declared, or one of a dropped clause: the user decides
        dropped = f" Tests of clauses no longer in the domain: {', '.join(orphans)}." if orphans else ""
        return [ask_routed(f"The full suite fails outside the units' tests: {event.payload['failure'][:500]}{dropped}",
                           "No unit's cycle declared the failing tests.", "suite-outside-units",
                           [action("reopen-unit", "Reopen a unit on one of its clauses, so that unclebob fixes it.",
                                   unit="the unit's id", clause="the clause's id"),
                            action("run-again", "Run the full suite again (the user fixed it, or it was transient)."),
                            action("go-on", "Go on to the static checks: the failing tests stay as they are.")],
                           BRANCH, event.id)]
    return [Command("ReopenUnit", "beck", {"unit": u, "clauses": sorted(set(cs)), "phase": "green",
                                           "reason": f"the full suite fails: {event.payload['failure'][:500]}"},
                    stream(u)) for u, cs in by_unit.items()]


@closing.policy("DecisionRouted")
def after_a_closing_question(event: Event, ctx: Ctx) -> list[Command]:
    situation, chosen, args = event.payload["situation"], event.payload["action"], event.payload["args"]
    if situation == "suite-outside-units":
        if chosen == "reopen-unit":
            clauses = {e.payload["clause"] for e in ctx.events(types=("CycleOpened",))
                       if e.payload["unit"] == args["unit"]}
            if args["clause"] not in clauses:   # kent named what the units do not have: the user again
                ask = ctx.store.get(event.payload["ask"])
                return [Command("Ask", "user", dict(ask.payload["args"]), ask.stream)]
            return [Command("ReopenUnit", "beck", {"unit": args["unit"], "clauses": [args["clause"]], "phase": "green",
                                                   "reason": f"the user's decision: {event.payload['why']}"},
                            stream(args["unit"]))]
        if chosen == "run-again":
            return [Command("RunFullSuite", "beck", {}, BRANCH)]
        return [Command("GoOn", "beck", {"as": "SuiteGreen", "payload": {}}, BRANCH)]
    if situation == "checks-blocked":
        if chosen == "run-again":
            return [Command("RunStaticChecks", "beck", {}, BRANCH)]
        return [Command("GoOn", "beck", {"as": "StaticChecksPassed", "payload": {}}, BRANCH)]
    return []


@closing.handler("ReopenUnit")
def handle_reopen_unit(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    unit = a["unit"]
    n = len([e for e in ctx.store.read(stream=stream(unit), types_=("UnitReopened",))]) + 2
    path = ctx.project / ".beck" / "worktrees" / unit
    branch = f"beck/{ctx.iteration[:8]}/{unit}-{n}"
    base = _head(ctx.project)
    if not path.exists():
        _git(ctx.project, "worktree", "add", "-q", "-b", branch, str(path), base)
    groups = {e.payload["clause"]: e.payload["group"] for e in ctx.store.read(types_=("CycleOpened",))
              if e.payload["unit"] == unit}
    results = [Result("UnitReopened", {"unit": unit, "worktree": str(path), "branch": branch, "base": base,
                                       "reason": a["reason"]})]
    for clause in a["clauses"]:
        results.append(Result("CycleReopened", {"unit": unit, "clause": clause, "group": groups.get(clause, "model"),
                                                "phase": a["phase"], "reason": a["reason"]},
                              stream=cycle_stream(unit, clause)))
    return results


@closing.policy("SuiteGreen")
def static_checks_of_the_branch(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunStaticChecks", "beck", {}, BRANCH)]


@closing.handler("RunStaticChecks")
def handle_static_checks(command: Event, ctx: Ctx) -> list[Result]:
    toolchain = load(ctx.project)
    if not toolchain.check:
        return [Result("StaticChecksPassed", {})]
    result = static_checks.run(ctx.project, toolchain, ["."], whole_project=True, actor="beck",
                               cause_id=command.id, iteration=ctx.iteration)
    if result.exit_code == 0:
        return [Result("StaticChecksPassed", {})]
    findings = [str(f) for r in result.results if r.findings for f in r.findings] or [result.summary]
    return [Result("StaticChecksFailed", {"findings": findings, "signature": signature(findings)})]


@closing.policy("StaticChecksFailed")
def fix_the_branch(event: Event, ctx: Ctx) -> list[Command]:
    sequence = [e.payload["signature"] for e in ctx.events(stream=BRANCH) if e.type == "StaticChecksFailed"]
    why = blocked(sequence, *ctx.limits)
    if why:
        return [ask_routed(f"The project's static checks do not pass ({why}): "
                           + "; ".join(event.payload["findings"][:20]), "martin's fixes repeat.", "checks-blocked",
                           [action("run-again", "Run the project's static checks again (the user fixed them)."),
                            action("go-on", "Go on to the review: the findings stay as they are.")],
                           BRANCH, event.id)]
    context = {"findings": event.payload["findings"], "check": "beck check --all"}
    return [Command("FixStaticFindings", "martin", {"session": "martin/branch", "context": context, "branch": True},
                    BRANCH)]


@closing.policy("BranchFixesDeclared")
def commit_branch_fixes(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CommitBranchFixes", "beck", {}, BRANCH)]


def blame_target(root: Path, file: str) -> str:
    """The commit that wrote most of the lines a change to file touches; HEAD for a new file."""
    diff = _git(root, "diff", "-U0", "HEAD", "--", file).stdout
    shas: Counter[str] = Counter()
    for line in diff.splitlines():
        if not line.startswith("@@"):
            continue
        old = line.split()[1][1:]  # -start,count
        start, _, count = old.partition(",")
        n = int(count) if count else 1
        first = max(int(start), 1)
        blame = _git(root, "blame", "--porcelain", "-L", f"{first},+{max(n, 1)}", "HEAD", "--", file, check=False)
        for b in blame.stdout.splitlines():
            parts = b.split()
            if parts and len(parts[0]) == 40 and len(parts) >= 3 and parts[0].isalnum():
                shas[parts[0]] += 1
    return shas.most_common(1)[0][0] if shas else _head(root)


@closing.handler("CommitBranchFixes")
def handle_branch_fixes(command: Event, ctx: Ctx) -> list[Result]:
    root = ctx.project
    tracked = _git(root, "diff", "--name-only", "HEAD", "--", ".", ":!.beck").stdout.split()
    new = _git(root, "ls-files", "--others", "--exclude-standard", "--", ".", ":!.beck").stdout.split()
    fixups = []
    for file in tracked + new:
        target = blame_target(root, file) if file in tracked else _head(root)
        _git(root, "add", "--", file)
        _git(root, "commit", "-q", "--fixup", target)
        fixups.append(f"{file} → {target[:8]}")
    return [Result("BranchFixesCommitted", {"fixups": fixups})]


@closing.policy("BranchFixesCommitted")
def check_again(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunStaticChecks", "beck", {}, BRANCH)]


def _rounds(ctx: Ctx) -> int:
    return len([e for e in ctx.events(types=("CommandIssued",)) if e.payload["command"] == "PresentForReview"])


@closing.policy("StaticChecksPassed")
def present_or_squash(event: Event, ctx: Ctx) -> list[Command]:
    if _rounds(ctx) == 0:
        return [Command("AutosquashBranch", "beck", {"then": "review"}, BRANCH)]
    return [_present(ctx)]


def _present(ctx: Ctx) -> Command:
    log = _git(ctx.project, "log", "--format=%h %s", f"{iteration_base(ctx)}..HEAD").stdout.splitlines()
    args: dict[str, Any] = {"commits": log, "round": _rounds(ctx) + 1}
    notes = [f"{e.payload['unit']}: the project's coverage {e.payload['before']} % → {e.payload['after']} %"
             for e in ctx.events(types=("CoverageDecreased",))]
    notes += [f"{e.payload['unit']}: lines no test runs, {_lines(e.payload['gaps'])}"
              for e in _last_coverage(ctx) if e.payload["gaps"]]
    notes += [f"flaky: {e.payload['test']} ({e.payload['evidence']})" for e in ctx.events(types=("FlakyTestFound",))]
    notes += [f"a stub no cycle implemented: {where}" for where in _stubs(ctx.project)]
    notes += [f"a clause no cycle proved: {c}" for c in unproved(ctx)]
    if notes:
        args["notes"] = notes
    return Command("PresentForReview", "user", args, BRANCH)


def unproved(ctx: Ctx) -> list[str]:
    """The planned clauses no cycle closed, as unit/clause: a resolution may add one to a unit that has landed."""
    done = {(e.payload["unit"], e.payload["clause"]) for e in ctx.events(types=("CycleClosed", "CycleDropped"))}
    return [f"{u}/{c}" for u, p in plan(ctx).items() for g in p["groups"] for c in g["clauses"] if (u, c) not in done]


def _stubs(root: Path) -> list[str]:
    """The interface bodies kent wrote that still raise "not implemented": no clause cited them, so no cycle
    implemented them, and the suite cannot tell."""
    toolchain = load(root)
    marker, suffix = STUBS[toolchain.language]
    found = []
    for base in toolchain.source_roots(root):
        for path in sorted((root / base).rglob(f"*{suffix}")):
            if ".beck" in path.relative_to(root).parts:
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if marker in line:
                    found.append(f"{path.relative_to(root)}:{n}")
    return found


def _lines(gaps: dict[str, list[int]]) -> str:
    return "; ".join(f"{f}: {', '.join(map(str, ls[:10]))}" for f, ls in gaps.items())


def _last_coverage(ctx: Ctx) -> list[Event]:
    last: dict[str, Event] = {}
    for e in ctx.events(types=("CoverageMeasured",)):
        if "unit" in e.payload:
            last[e.payload["unit"]] = e
    return list(last.values())


@closing.handler("AutosquashBranch")
def handle_autosquash_branch(command: Event, ctx: Ctx) -> list[Result]:
    autosquash_onto(ctx.project, squash_base(ctx))
    results = [Result("BranchSquashed", {"sha": _head(ctx.project)})]
    if command.payload["args"]["then"] == "close":
        Project(ctx.project).end_iteration(ctx.iteration)
        results.append(Result("IterationClosed", {}))
    return results


@closing.policy("BranchSquashed")
def present(event: Event, ctx: Ctx) -> list[Command]:
    issued = ctx.store.get(event.causation_id or "")
    if issued.payload["args"]["then"] != "review":
        return []
    return [_present(ctx)]


@closing.policy("CorrectionsRequested")
def plan_corrections(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("PlanCorrections", "kent", {"session": f"corrections/{event.id[:8]}",
                                                "context": {"request": event.payload["text"]}}, BRANCH)]


@closing.translator("PlanCorrections")
def corrections_planned(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("CorrectionsPlanned", {"plan": output["result"]["plan"]}, actor="kent")]


@closing.policy("CorrectionsPlanned")
def correct_units(event: Event, ctx: Ctx) -> list[Command]:
    commands = []
    for i, item in enumerate(event.payload["plan"], 1):
        reason = f"the user's correction: {item['description']}"
        if item["agent"] == "unclebob":
            clauses = item.get("clauses") or sorted({c for t, c in _declared_tests(ctx, item["unit"]).items()})[:1]
            commands.append(Command("ReopenUnit", "beck", {"unit": item["unit"], "clauses": clauses, "phase": "red",
                                                           "reason": reason}, stream(item["unit"])))
        else:
            commands.append(Command("ReopenForMartin", "beck", {"unit": item["unit"], "id": f"C{i}",
                                                                "description": item["description"]},
                                    stream(item["unit"])))
    return commands


@closing.handler("ReopenForMartin")
def handle_reopen_for_martin(command: Event, ctx: Ctx) -> list[Result]:
    """A correction for martin: the unit reopens with a review holding one finding, the correction."""
    a = command.payload["args"]
    reopened = handle_reopen_unit(Event(**{**command.__dict__, "payload": {
        **command.payload, "args": {"unit": a["unit"], "clauses": [], "phase": "green",
                                    "reason": f"the user's correction: {a['description']}"}}}), ctx)
    tree = Path(reopened[0].payload["worktree"])
    files = _git(tree, "ls-files", "--", *ctx.workspace.scope(a["unit"])).stdout.split()
    files.sort(key=lambda f: (tree / f).stat().st_size if (tree / f).is_file() else 0, reverse=True)
    finding = {"id": a["id"], "kind": "readability", "file": files[0] if files else ".",  # a hint: the largest
               "description": a["description"], "proposal": a["description"]}
    return reopened + [Result("UnitReviewed", {"unit": a["unit"], "findings": [finding], "weak_tests": []})]


@closing.policy("ReviewAccepted")
def close(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("AutosquashBranch", "beck", {"then": "close"}, BRANCH)]


workflow = units_machine.merge(closing)
