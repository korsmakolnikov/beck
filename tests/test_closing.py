"""Phase 6 on a real repository: the full suite, the project's checks, the reviews and the close."""

import sys

from test_refactor import CHECKER, HEADS, Team, out
from test_unit import git, pending_of_user, plan, repo, run  # noqa: F401

from beck.events.project import Project
from beck.machine.closing import workflow
from beck.machine.core import Ctx, Reactor, Result
from beck.machine.unit import Workspace


class Reviewers(Team):
    def PlanCorrections(self, envelope, cwd):
        return out("PlanCorrections", result={"plan": [{"unit": "store", "agent": "martin",
                                                         "description": "Write the one as 2 - 1."}]})


def setup(repo, agents, toolchain_extra=""):
    if toolchain_extra:
        path = repo / ".beck/toolchain.yml"
        path.write_text(path.read_text().replace("check: []\n", "") + toolchain_extra)
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace(store, repo, lambda unit, tree: [f"src/shop/{unit}"],
                          lambda unit, group: {"unit": {"id": unit}, "group": group, "clauses": [], "entities": [],
                                               "dependencies": [], "files": {"source": [], "tests": []}})
    ctx = Ctx(store, repo, it.id, workspace, units_in_parallel=2)
    store.append("UnitsPlanned", plan())
    return Reactor(ctx, workflow, agents), store, project, it


def review(reactor):
    [r] = [c for c in pending_of_user(reactor) if c.payload["command"] == "PresentForReview"]
    return r


def branch(repo):
    return git(repo, "log", "--format=%s").splitlines()


def test_the_branch_is_presented_squashed_and_closed_on_acceptance(repo):
    reactor, store, project, it = setup(repo, Reviewers(repo))
    run(reactor)
    r = review(reactor)
    assert [c.split(" ", 1)[1] for c in r.payload["args"]["commits"]] == [HEADS["cart"], HEADS["store"]]
    assert [e.type for e in store.read(stream="branch") if e.type in ("SuiteGreen", "StaticChecksPassed",
                                                                       "BranchSquashed")] == [
        "SuiteGreen", "StaticChecksPassed", "BranchSquashed"]
    reactor.answer(r.id, [Result("ReviewAccepted", {}, actor="user")])
    run(reactor)
    assert [e.type for e in store.read(types_=("IterationClosed",))] == ["IterationClosed"]
    assert project.iteration(it.id).ended_at is not None
    assert branch(repo) == [HEADS["cart"], HEADS["store"], "interfaces"]


def test_the_project_s_checks_are_fixed_as_fixups_and_folded_before_the_review(repo):
    (repo / "checker.py").write_text(CHECKER)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "checker")

    def with_todo(team, envelope, cwd):
        result = team.WriteGreenCode(envelope, cwd)
        path = cwd / "src/shop/store/store_adds.py"
        path.write_text(path.read_text().replace("return 1", "return 1  # TODO"))
        team.test(cwd, "store", "store-adds")
        return result
    team = Reviewers(repo, {("WriteGreenCode", "store-adds"): [with_todo]})
    reactor, store, *_ = setup(repo, team, toolchain_extra=(
        f"check:\n  - {{name: todo, command: [{sys.executable}, checker.py, src], format: text, scope: project}}\n"))
    run(reactor)
    [failed] = store.read(types_=("StaticChecksFailed",))
    assert failed.payload["findings"][0].startswith("src/shop/store/store_adds.py:2:1")
    [fixed] = store.read(types_=("BranchFixesCommitted",))
    assert fixed.payload["fixups"][0].startswith("src/shop/store/store_adds.py → ")
    r = review(reactor)
    assert [c.split(" ", 1)[1] for c in r.payload["args"]["commits"]] == [HEADS["cart"], HEADS["store"]]
    assert "TODO" not in (repo / "src/shop/store/store_adds.py").read_text()


def test_a_correction_is_a_fixup_the_next_review_shows_and_the_close_folds(repo):
    reactor, store, project, it = setup(repo, Reviewers(repo))
    run(reactor)
    reactor.answer(review(reactor).id, [Result("CorrectionsRequested", {"text": "store: write 2 - 1"}, actor="user")])
    run(reactor)
    second = review(reactor)
    assert second.payload["args"]["round"] == 2
    assert [c.split(" ", 1)[1] for c in second.payload["args"]["commits"]] == [
        f"fixup! {HEADS['store']}", HEADS["cart"], HEADS["store"]]
    assert "return 2 - 1" in (repo / "src/shop/store/store_adds.py").read_text()
    reactor.answer(second.id, [Result("ReviewAccepted", {}, actor="user")])
    run(reactor)
    assert branch(repo) == [HEADS["cart"], HEADS["store"], "interfaces"]
    assert "return 2 - 1" in git(repo, "show", "HEAD~1:src/shop/store/store_adds.py")


def test_a_failing_full_suite_reopens_the_unit_of_the_failing_test(repo):
    reactor, store, *_ = setup(repo, Reviewers(repo))
    run(reactor)
    first = review(reactor)
    # the cart's code breaks on the feature branch: the next full suite finds it
    (repo / "src/shop/cart/cart_totals.py").write_text("def cart_totals():\n    return 2\n")
    git(repo, "commit", "-qam", "a breaking change")
    reactor.answer(first.id, [Result("CorrectionsRequested", {"text": "store: write 2 - 1"}, actor="user")])
    run(reactor)
    failed = list(store.read(types_=("SuiteFailed",)))
    assert failed and failed[0].payload["tests"] == ["tests/test_cart_totals.py::test_cart_totals"]
    reopened = [e.payload["unit"] for e in store.read(types_=("UnitReopened",))]
    assert sorted(set(reopened)) == ["cart", "store"]
    assert "return 1" in (repo / "src/shop/cart/cart_totals.py").read_text()
    assert review(reactor).payload["args"]["round"] == 2


class Router(Reviewers):
    """kent routes the user's words: first an action outside the menu, then go-on."""

    def RouteDecision(self, envelope, cwd):
        ctx = envelope["context"]
        assert ctx["situation"] == "suite-outside-units" and ctx["answer"] == "It is a known failure, go on."
        assert [a["id"] for a in ctx["actions"]] == ["reopen-unit", "run-again", "go-on"]
        if "retry" not in envelope:
            return out("RouteDecision", result={"action": "skip", "args": {}, "why": "the user says so"})
        assert "'skip' is not in the menu" in envelope["retry"]["reason"]
        return out("RouteDecision", result={"action": "go-on", "args": {}, "why": "a known failure"})


def test_beck_s_own_question_is_routed_by_kent_to_an_action_of_its_menu(repo):
    from beck.machine import user
    reactor, store, *_ = setup(repo, Router(repo))
    run(reactor)
    first = review(reactor)
    (repo / "tests/test_legacy.py").write_text("def test_legacy():\n    assert False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a legacy test")
    reactor.answer(first.id, [Result("CorrectionsRequested", {"text": "store: write 2 - 1"}, actor="user")])
    run(reactor)
    [ask] = [c for c in pending_of_user(reactor) if c.payload["command"] == "Ask"]
    assert "outside the units' tests" in user.prompt(ask)
    reactor.answer(ask.id, user.answer(ask, "It is a known failure, go on."))
    run(reactor)
    assert [e.payload["action"] for e in store.read(types_=("DecisionRouted",))] == ["go-on"]
    assert len(list(store.read(types_=("RouteRejected",)))) == 1
    green = list(store.read(types_=("SuiteGreen",)))[-1]
    assert green.actor == "user"
    assert review(reactor).payload["args"]["round"] == 2
