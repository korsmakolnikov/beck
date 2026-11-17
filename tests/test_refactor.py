"""Phase 5 on a real repository: beck runs the component's tests, squashes, martin reviews and refactors, beck lands."""

import sys

from test_component import Agents, component_events, execution, fn, git, pending_of_user, repo, run  # noqa: F401

from beck.events.project import Project
from beck.machine.component import Workspace
from beck.machine.core import Ctx, Engine, Result
from beck.machine.refactor import components_machine

HEADS = {"store": "feat(store): implement store", "cart": "feat(cart): implement cart"}


def out(command, outcome="done", **body):
    return {"protocol": 1, "command": command, "outcome": outcome, **body}


class Team(Agents):
    """unclebob and kent as in test_component; martin finds nothing by default, and refactors what it is given."""

    async def turn(self, agent, session, envelope, env, cwd):
        self.session = session
        return await super().turn(agent, session, envelope, env, cwd)

    def _play(self, agent, envelope, cwd):
        if agent == "martin":
            key = self.session.split("/")[1]
            steps = self.script.get((envelope["command"], key))
            if steps:
                n = sum(1 for a, e in self.calls if a == "martin" and e["command"] == envelope["command"])
                step = steps[min(n, len(steps)) - 1]
                if step is not None:
                    return step(self, envelope, cwd)
            return getattr(self, envelope["command"])(envelope, cwd)
        return super()._play(agent, envelope, cwd)

    def ReviewComponent(self, envelope, cwd):
        return out("ReviewComponent", result={"findings": [], "weak_tests": []})

    def RefactorComponent(self, envelope, cwd):
        finding = envelope["context"]["findings"][0]
        path = cwd / finding["file"]
        path.write_text(path.read_text().replace("return 1", "return 2 - 1"))
        return out("RefactorComponent", result={"phase": "code", "addressed": [finding["id"]]})

    def FixStaticFindings(self, envelope, cwd):
        for line in envelope["context"]["findings"]:
            path = cwd / line.split(":")[0]
            path.write_text(path.read_text().replace("  # TODO", ""))
        return out("FixStaticFindings")


def finding(component, requirement, fid="F1"):
    return {"id": fid, "kind": "readability", "file": f"src/shop/{component}/{fn(requirement)}.py", "line": 2,
            "description": "a magic number", "proposal": "name it"}


def setup(repo, agents, scope=None, toolchain_extra=""):
    if toolchain_extra:
        path = repo / ".beck/toolchain.yml"
        text = path.read_text().replace("check: []\n", "") if "check:" in toolchain_extra else path.read_text()
        path.write_text(text + toolchain_extra)
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace(store, repo, scope or (lambda component, tree: [f"src/shop/{component}"]),
                          lambda component, unit: {"component": {"id": component}, "unit": unit, "requirements": [],
                                                   "entities": [],
                                               "dependencies": [], "files": {"source": [], "tests": []}},
                          lambda requirement: f"The sentence of {requirement}.")
    ctx = Ctx(store, repo, it.id, workspace, components_in_parallel=2)
    engine = Engine(ctx, components_machine, agents)
    engine.record("ExecutionDerived", execution())
    return engine, store


def feature_log(repo):
    return git(repo, "log", "--format=%s").splitlines()


def test_components_land_squashed_in_dependency_order_and_close(repo):
    team = Team(repo)
    engine, store = setup(repo, team)
    run(engine)
    assert feature_log(repo) == [HEADS["cart"], HEADS["store"], "interfaces"]
    for component in ("store", "cart"):
        assert component_events(store, component)[-6:] == ["ComponentMeasured", "ComponentReviewed", "ChecksPassed",
                                                           "ComponentReady",
                                                 "ComponentLanded", "ComponentClosed"]
        assert not (repo / f".beck/worktrees/{component}").exists()
    landed = [e.payload["component"] for e in store.read(types_=("ComponentLanded",))]
    assert landed == ["store", "cart"]
    review = next(e for a, e in team.calls if a == "martin")
    assert review["context"]["requirements"][0]["text"].startswith("The sentence of")
    assert review["context"]["measures"]["functions"] >= 1
    assert "Requirements: cart-totals, cart-empties" in git(repo, "log", "-1", "--format=%b")


def test_a_refactor_is_a_fixup_the_autosquash_folds_in(repo):
    team = Team(repo, {("ReviewComponent", "store"): [
        lambda t, e, c: out("ReviewComponent", result={"findings": [finding("store", "store-adds")], "weak_tests": []})]})
    engine, store = setup(repo, team)
    run(engine)
    assert [e.type for e in store.read(types_=("RefactorCommitted",))] == ["RefactorCommitted"]
    assert feature_log(repo) == [HEADS["cart"], HEADS["store"], "interfaces"]   # no fixup reaches the branch
    assert "return 2 - 1" in (repo / "src/shop/store/store_adds.py").read_text()


def test_a_refactor_that_breaks_its_phase_is_reverted_and_sent_back(repo):
    def touches_a_test(team, envelope, cwd):
        (cwd / "tests/test_store_adds.py").write_text((cwd / "tests/test_store_adds.py").read_text() + "\n")
        return team.RefactorComponent(envelope, cwd)
    team = Team(repo, {("ReviewComponent", "store"): [
        lambda t, e, c: out("ReviewComponent", result={"findings": [finding("store", "store-adds")], "weak_tests": []})],
        ("RefactorComponent", "store"): [touches_a_test, None]})
    engine, store = setup(repo, team)
    run(engine)
    [rejected] = store.read(types_=("RefactorRejected",))
    assert rejected.payload["reason"] == "a refactor of the code changes no test; changed: tests/test_store_adds.py"
    retry = [e for a, e in team.calls if e["command"] == "RefactorComponent"][-1]
    assert retry["retry"]["attempt"] == 2
    assert component_events(store, "store")[-1] == "ComponentClosed"


def test_a_finding_already_met_closes_with_no_commit(repo):
    # the replay of 2026-11-16: martin found F1 already fixed and had no way to say so, seven times
    team = Team(repo, {("ReviewComponent", "store"): [
        lambda t, e, c: out("ReviewComponent", result={"findings": [finding("store", "store-adds")], "weak_tests": []})],
        ("RefactorComponent", "store"): [lambda t, e, c: out("RefactorComponent", result={
            "phase": "none", "addressed": ["F1"], "why": "HEAD already names the number."})]})
    engine, store = setup(repo, team)
    run(engine)
    assert not list(store.read(types_=("RefactorRejected",)))
    [committed] = store.read(types_=("RefactorCommitted",))
    assert committed.payload["addressed"] == ["F1"]
    assert component_events(store, "store")[-1] == "ComponentClosed"


def test_a_malformed_refactor_is_asked_again_once(repo):
    # the replay of 2026-11-16: the generic retry and the refactor's both answered a rejection, doubling the turns
    team = Team(repo, {("ReviewComponent", "store"): [
        lambda t, e, c: out("ReviewComponent", result={"findings": [finding("store", "store-adds")], "weak_tests": []})],
        ("RefactorComponent", "store"): [lambda t, e, c: out("RefactorComponent", result={"phase": "blue"}), None]})
    engine, store = setup(repo, team)
    run(engine)
    assert len([e for a, e in team.calls if e["command"] == "RefactorComponent"]) == 2
    assert component_events(store, "store")[-1] == "ComponentClosed"


def test_a_worsened_measure_waits_for_the_user(repo):
    def branches(team, envelope, cwd):
        (cwd / "src/shop/store/store_adds.py").write_text(
            "def store_adds(x=None):\n    if x is None:\n        return 1\n    if x:\n        return 1\n    return 1\n")
        return out("RefactorComponent", result={"phase": "code", "addressed": ["F1"]})
    team = Team(repo, {("ReviewComponent", "store"): [
        lambda t, e, c: out("ReviewComponent", result={"findings": [finding("store", "store-adds")], "weak_tests": []})],
        ("RefactorComponent", "store"): [branches]})
    engine, store = setup(repo, team)
    run(engine)
    [worse] = store.read(types_=("MeasureWorsened",))
    assert worse.payload["worsened"][0].startswith("ccn_max 1 → 3")
    [ask] = [c for c in pending_of_user(engine) if c.payload["command"] == "ApproveTradeoff"]
    assert component_events(store, "store")[-1] != "ComponentLanded"
    engine.answer(ask.id, [Result("TradeoffAccepted", {}, actor="user")])
    run(engine)
    assert component_events(store, "store")[-1] == "ComponentClosed"
    assert "if x is None" in (repo / "src/shop/store/store_adds.py").read_text()


def test_a_regression_reopens_the_cycle_of_the_failing_test(repo):
    def breaks_totals(team, envelope, cwd):
        (cwd / "src/shop/cart/cart_totals.py").write_text("def cart_totals():\n    return 2\n")
        return team.WriteGreenCode(envelope, cwd)
    team = Team(repo, {("WriteGreenCode", "cart-empties"): [breaks_totals]})
    engine, store = setup(repo, team)
    run(engine)
    [regression] = store.read(types_=("RegressionFound",))
    assert regression.payload["tests"] == ["tests/test_cart_totals.py::test_cart_totals"]
    reopened = [e for e in store.read(types_=("CycleReopened",))]
    assert [(e.payload["requirement"], e.payload["phase"]) for e in reopened] == [("cart-totals", "green")]
    assert component_events(store, "cart")[-1] == "ComponentClosed"
    assert "return 1" in (repo / "src/shop/cart/cart_totals.py").read_text()


def test_a_failing_test_of_no_cycle_asks_for_a_restart_instead_of_stalling(repo):
    # the session of 2026-11-15: a failing test that references no scheduled requirement parked the component with no way out,
    # and the session stopped with nothing to do and nothing to answer
    team = Team(repo)
    engine, store = setup(repo, team)
    run(engine)
    engine.record("RegressionFound", {"component": "cart", "tests": ["tests/fitness/test_stray.py::test_stray"],
                                     "failure": "NOT_OK (1 of 3 not passed)"}, stream="component:cart")
    run(engine)
    assert [e.payload["component"] for e in store.read(types_=("RegressionUnassigned",))] == ["cart"]
    proposals = [e for a, e in team.calls if e["command"] == "ProposeRestart"]
    assert proposals and proposals[-1]["context"]["kind"] == "blocked_cycle"
    assert [c.payload["command"] for c in engine.pending() if c.payload["target"] == "user"] == ["ApproveRestart"]


CHECKER = """import sys
bad = [f"{p}:2:1 X1 a TODO is left" for p in sys.argv[1:] for p in ([p] if p.endswith('.py') else [])]
import pathlib
found = [f"{f}:{i}:1: a TODO is left" for a in sys.argv[1:] for f in sorted(pathlib.Path(a).rglob('*.py'))
         for i, line in enumerate(f.read_text().splitlines(), 1) if 'TODO' in line]
print("\\n".join(found))
sys.exit(1 if found else 0)
"""


def test_the_static_checks_go_to_martin_before_the_autosquash(repo):
    (repo / "checker.py").write_text(CHECKER)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "checker")

    def with_todo(team, envelope, cwd):
        result = team.WriteGreenCode(envelope, cwd)
        path = cwd / "src/shop/store/store_adds.py"
        path.write_text(path.read_text().replace("return 1", "return 1  # TODO"))
        team.test(cwd, "store", "store-adds")
        return result
    team = Team(repo, {("WriteGreenCode", "store-adds"): [with_todo]})
    engine, store = setup(repo, team, toolchain_extra=f"check:\n  - {{name: todo, command: [{sys.executable}, "
                                                       f"checker.py, '{{paths}}'], format: text}}\n")
    run(engine)
    [failed] = store.read(types_=("ChecksFailed",))
    assert failed.payload["findings"][0].startswith("src/shop/store/store_adds.py:2:1")
    assert [e["command"] for a, e in team.calls if a == "martin" and e["command"] == "FixStaticFindings"]
    assert component_events(store, "store")[-1] == "ComponentClosed"
    assert "TODO" not in (repo / "src/shop/store/store_adds.py").read_text()


def test_a_landing_conflict_goes_to_martin(repo):
    shared = "src/shop/shared.py"
    (repo / shared).write_text("NAMES = []\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "shared")

    def and_shared(name):
        def step(team, envelope, cwd):
            (cwd / shared).write_text(f"NAMES = [{name!r}]\n")
            return team.WriteGreenCode(envelope, cwd)
        return step

    def merge(team, envelope, cwd):
        assert envelope["context"]["files"] == [shared] and envelope["context"]["landed"] == ["store"]
        (cwd / shared).write_text("NAMES = ['store', 'cart']\n")
        return out("ResolveConflict")
    team = Team(repo, {("WriteGreenCode", "store-adds"): [and_shared("store")],
                       ("WriteGreenCode", "cart-totals"): [and_shared("cart")],
                       ("ResolveConflict", "cart"): [merge]})
    engine, store = setup(repo, team, scope=lambda component, tree: [f"src/shop/{component}", shared])
    run(engine)
    [conflict] = store.read(types_=("LandingConflicted",))
    assert conflict.payload == {"component": "cart", "files": [shared], "signature": conflict.payload["signature"]}
    assert feature_log(repo) == [HEADS["cart"], HEADS["store"], "shared", "interfaces"]
    assert (repo / shared).read_text() == "NAMES = ['store', 'cart']\n"


COVERAGE = 'coverage: ["--cov={path}", "--cov-report=json:{coverage}"]\n'


def test_a_line_no_requirement_names_never_stops_the_component_and_goes_to_martin(repo):
    def extra(team, envelope, cwd):
        result = team.WriteGreenCode(envelope, cwd)
        (cwd / "src/shop/store/unused.py").write_text("def unused():\n    return 3\n")
        team.test(cwd, "store", "store-adds")
        return result
    team = Team(repo, {("WriteGreenCode", "store-adds"): [extra]})
    engine, store = setup(repo, team, toolchain_extra=COVERAGE)
    run(engine)
    measured = [e for e in store.read(types_=("CoverageMeasured",)) if e.payload["component"] == "store"]
    # the doctest collection imports the module: its def line runs, its body does not
    assert measured[-1].payload["gaps"] == {"src/shop/store/unused.py": [2]}
    assert not list(store.read(types_=("CoverageGapsFound",)))
    review = next(e for a, e in team.calls if a == "martin" and e["context"]["component"]["id"] == "store")
    assert review["context"]["uncovered"] == {"src/shop/store/unused.py": [2]}
    assert component_events(store, "store")[-1] == "ComponentClosed"


def branchy(team, envelope, cwd):
    """The green code of store-adds, with a branch its test does not run, in the requirement's marked function."""
    (cwd / "src/shop/store/store_adds.py").write_text(
        "def store_adds(x=None):\n    # spec: store-adds\n    if x is not None:\n        return x\n    return 1\n")
    team.test(cwd, "store", "store-adds")
    return out("WriteGreenCode")


def covers(team, envelope, cwd):
    [requirement] = envelope["context"]["requirements"]
    assert requirement["id"] == "store-adds" and requirement["lines"] == {"src/shop/store/store_adds.py": [4]}
    assert requirement["tests"] == ["tests/test_store_adds.py::test_store_adds"]
    (cwd / "tests/test_store_adds_given.py").write_text(
        "from shop.store.store_adds import store_adds\n\n\n"
        "def test_store_adds_given():\n    # spec: store-adds\n    assert store_adds(2) == 2\n")
    return out("CoverLines", result={"tests": [{"test": "tests/test_store_adds_given.py::test_store_adds_given",
                                                 "requirement": "store-adds"}]})


def test_a_line_a_requirement_names_is_covered_by_unclebob_once(repo):
    team = Team(repo, {("WriteGreenCode", "store-adds"): [branchy], ("CoverLines", "answer"): [covers]})
    engine, store = setup(repo, team, toolchain_extra=COVERAGE)
    run(engine)
    [found] = store.read(types_=("CoverageGapsFound",))
    assert found.payload["requirements"] == {"store-adds": {"src/shop/store/store_adds.py": [4]}}
    measured = [e for e in store.read(types_=("CoverageMeasured",)) if e.payload["component"] == "store"]
    assert [m.payload["gaps"] for m in measured][:2] == [{"src/shop/store/store_adds.py": [4]}, {}]
    assert component_events(store, "store")[-1] == "ComponentClosed"
    assert (repo / "tests/test_store_adds_given.py").is_file()      # landed with the component, squashed into it
    assert HEADS["store"] in feature_log(repo)


def test_covering_that_fails_twice_gives_up_and_goes_on(repo):
    def touches_the_code(team, envelope, cwd):
        (cwd / "src/shop/store/store_adds.py").write_text((cwd / "src/shop/store/store_adds.py").read_text() + "\n")
        return out("CoverLines", result={"tests": [{"test": "tests/test_store_adds.py::test_store_adds",
                                                     "requirement": "store-adds"}]})
    team = Team(repo, {("WriteGreenCode", "store-adds"): [branchy], ("CoverLines", "answer"): [touches_the_code]})
    engine, store = setup(repo, team, toolchain_extra=COVERAGE)
    run(engine)
    assert [e.payload["reason"][:30] for e in store.read(types_=("CoverageRejected",))] == \
        ["covering changes no source; ch"] * 2
    assert len(list(store.read(types_=("CoverageGivenUp",)))) == 1
    assert component_events(store, "store")[-1] == "ComponentClosed"


class Decider(Team):
    """kent routes the user's words on the coverage to go-on."""

    def RouteDecision(self, envelope, cwd):
        assert envelope["context"]["situation"] == "coverage-decreased"
        assert [a["id"] for a in envelope["context"]["actions"]] == ["cover-again", "go-on", "park-component"]
        return out("RouteDecision", result={"action": "go-on", "args": {}, "why": "a branch for later"})


def test_a_lower_coverage_asks_the_user_after_the_one_try(repo):
    from beck.machine import user
    asks_a_change = [lambda t, e, c: out("CoverLines", "change_requested", change={
        "kind": "interface", "target": "store-adds", "problem": "no", "proposal": "no"})]
    team = Decider(repo, {("WriteGreenCode", "store-adds"): [branchy], ("CoverLines", "answer"): asks_a_change})
    engine, store = setup(repo, team, toolchain_extra=COVERAGE)
    engine.record("CoverageMeasured", {"files": {"src/shop/store/store_adds.py": [2, 2]}, "gaps": {}})
    run(engine)
    [decreased] = store.read(types_=("CoverageDecreased",))
    assert decreased.payload["before"] == 100.0 and decreased.payload["after"] < 100.0
    [ask] = [c for c in pending_of_user(engine) if c.payload["command"] == "Ask"]
    assert "lowers the project's coverage from 100.0 %" in user.prompt(ask)
    assert component_events(store, "store")[-1] == "CoverageDecreased"     # it waits for the user
    engine.answer(ask.id, user.answer(ask, "Go on, I will cover it later."))
    run(engine)
    assert component_events(store, "store")[-1] == "ComponentClosed"


def test_a_component_with_no_commit_of_its_own_squashes_into_an_empty_one(repo):
    # the session of 2026-11-16: the component clock implements no requirement, so it had no commit to squash, and
    # SquashComponent crashed on an empty commit
    from beck.events.project import Project
    from beck.machine.component import Workspace
    from beck.machine.core import Ctx, Issued
    from beck.machine.refactor import handle_squash
    _, store, started = Project(repo).start_iteration()
    base = git(repo, "rev-parse", "HEAD")
    store.append("ComponentStarted", {"component": "clock", "worktree": str(repo), "branch": "b", "base": base},
                 stream="component:clock")
    workspace = Workspace(store, repo, lambda u, t: [], lambda u, g: {})
    command = Issued({"command": "SquashComponent", "target": "beck", "args": {"component": "clock"}},
                     "component:clock", started)
    [squashed] = handle_squash(command, Ctx(store, repo, "it", workspace))
    assert git(repo, "log", "-1", "--format=%s") == "feat(clock): implement clock"
    assert git(repo, "rev-parse", "HEAD~1") == base and squashed.payload["sha"] == git(repo, "rev-parse", "HEAD")


TOTAL = ('def total(prices):\n    """The sum of the prices.\n\n    spec: store-adds\n\n    >>> total([1, 2])\n    3\n'
         '    """\n    raise NotImplementedError\n')


def test_a_doctest_on_a_unimplemented_reopens_the_cycle_of_the_requirement_that_references_it(repo):
    # the session of 2026-11-16: kent's doctests found Score.add and Cannon.y still unimplemented once the component was
    # implemented; the failure went to kent, who writes no code, as a regression no cycle owned
    (repo / "src/shop/store/total.py").write_text(TOTAL)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the interface of total")
    def implements_total(team, envelope, cwd):
        (cwd / "src/shop/store/total.py").write_text(TOTAL.replace("raise NotImplementedError", "return sum(prices)"))
        return team.WriteGreenCode(envelope, cwd)
    team = Team(repo, {("WriteGreenCode", "store-adds"): [None, implements_total]})
    engine, store = setup(repo, team)
    run(engine)
    [reopened] = [e for e in store.read(types_=("CycleReopened",)) if e.payload["component"] == "store"]
    assert reopened.payload["requirement"] == "store-adds" and reopened.payload["phase"] == "green"
    assert "src/shop/store/total.py::shop.store.total.total" in reopened.payload["reason"]
    assert not list(store.read(types_=("RegressionUnassigned",)))
    assert component_events(store, "store")[-1] == "ComponentClosed"


def test_an_unimplemented_left_when_the_cycles_end_reopens_the_cycle_of_the_requirement_referencing_it(repo):
    # the session of 2026-11-16: main() referenced three morphisms, but no test needed it: their greens wrote other
    # functions, and the program still raised NotImplementedError when the component closed
    (repo / "src/shop/store/total.py").write_text('def total(prices):\n    """The sum.\n\n    spec: store-adds\n    """\n'
                                                  '    raise NotImplementedError\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the interface of total")
    def implements_total(team, envelope, cwd):
        (cwd / "src/shop/store/total.py").write_text('def total(prices):\n    """The sum.\n\n    spec: store-adds\n'
                                                     '    """\n    return sum(prices)\n')
        return team.WriteGreenCode(envelope, cwd)
    team = Team(repo, {("WriteGreenCode", "store-adds"): [None, implements_total]})
    engine, store = setup(repo, team)
    run(engine)
    [reopened] = [e for e in store.read(types_=("CycleReopened",)) if e.payload["component"] == "store"]
    assert reopened.payload["requirement"] == "store-adds"
    assert "an unimplemented left: src/shop/store/total.py:6" in reopened.payload["reason"]
    assert component_events(store, "store")[-1] == "ComponentClosed"

