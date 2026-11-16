"""Phase 6 on a real repository: the full suite, the project's checks, the reviews and the close."""

import sys

from test_component import execution, git, pending_of_user, repo, run  # noqa: F401
from test_refactor import CHECKER, HEADS, Team, out

from beck.events.project import Project
from beck.machine.closing import workflow
from beck.machine.component import Workspace
from beck.machine.core import Ctx, Reactor, Result


class Reviewers(Team):
    def PlanCorrections(self, envelope, cwd):
        return out("PlanCorrections", result={"plan": [{"component": "store", "agent": "martin",
                                                         "description": "Write the one as 2 - 1."}]})


def setup(repo, agents, toolchain_extra=""):
    if toolchain_extra:
        path = repo / ".beck/toolchain.yml"
        path.write_text(path.read_text().replace("check: []\n", "") + toolchain_extra)
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace(store, repo, lambda component, tree: [f"src/shop/{component}"],
                          lambda component, unit: {"component": {"id": component}, "unit": unit, "requirements": [],
                                                   "entities": [],
                                               "dependencies": [], "files": {"source": [], "tests": []}})
    ctx = Ctx(store, repo, it.id, workspace, components_in_parallel=2)
    store.append("ExecutionDerived", execution())
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


def test_a_failing_full_suite_reopens_the_component_of_the_failing_test(repo):
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
    reopened = [e.payload["component"] for e in store.read(types_=("ComponentReopened",))]
    assert sorted(set(reopened)) == ["cart", "store"]
    assert "return 1" in (repo / "src/shop/cart/cart_totals.py").read_text()
    assert review(reactor).payload["args"]["round"] == 2


class Router(Reviewers):
    """kent routes the user's words: first an action outside the menu, then go-on."""

    def RouteDecision(self, envelope, cwd):
        ctx = envelope["context"]
        assert ctx["situation"] == "suite-outside-components" and ctx["answer"] == "It is a known failure, go on."
        assert [a["id"] for a in ctx["actions"]] == ["reopen-component", "run-again", "go-on"]
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
    assert "outside the components' tests" in user.prompt(ask)
    reactor.answer(ask.id, user.answer(ask, "It is a known failure, go on."))
    run(reactor)
    assert [e.payload["action"] for e in store.read(types_=("DecisionRouted",))] == ["go-on"]
    assert len(list(store.read(types_=("RouteRejected",)))) == 1
    green = list(store.read(types_=("SuiteGreen",)))[-1]
    assert green.actor == "user"
    assert review(reactor).payload["args"]["round"] == 2


def test_a_failing_test_of_a_dropped_requirement_reopens_no_component(repo):
    # the session of 2026-11-15: the full suite failed on the test of a requirement taken out of the domain, and beck
    # reopened the component on that requirement, four times
    from test_component import without

    from beck.machine.closing import reopen_failing
    store = Project(repo).start_iteration()[1]
    store.append("ExecutionDerived", without("cart-totals"))
    store.append("RedVerified", {"component": "cart", "requirement": "cart-totals", "test": "tests/test_t.py::test_t",
                                 "run": "r"}, stream="cycle:cart/cart-totals")
    failed = store.append("SuiteFailed", {"tests": ["tests/test_t.py::test_t"], "failure": "NOT_OK"}, stream="branch")
    [command] = reopen_failing(failed, Ctx(store, repo, "it", None))
    assert command.name == "Ask" and "no longer in the domain: tests/test_t.py::test_t" in \
        command.args["question"]["text"]


def test_the_review_names_the_interfaces_no_cycle_implemented(repo):
    # the session of 2026-11-15: the suite was green, but the program's entry point, which no requirement referenced, still
    # raised "not implemented": the command did not start, and the review did not say so
    (repo / "src/shop/main.py").write_text('def main():\n    """The entry point."""\n'
                                           '    raise NotImplementedError("not implemented")\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the entry point's interface")
    reactor, store, *_ = setup(repo, Reviewers(repo))
    run(reactor)
    assert "an unimplemented no cycle implemented: src/shop/main.py:3" in review(reactor).payload["args"]["notes"]


def test_the_review_names_the_scheduled_requirements_no_cycle_proved(repo):
    # the session of 2026-11-15: a restart added a requirement to a component already landed; the new execution held it, but
    # no cycle ran it, the code never enforced it, and the program broke it: the match could last forever
    from test_component import execution as component_execution

    from beck.machine.closing import unproved
    store = Project(repo).start_iteration()[1]
    later = component_execution()
    later["components"][0]["units"][0]["requirements"].append("store-refuses-negatives")
    store.append("ExecutionDerived", later)
    for component, requirement in (("store", "store-adds"), ("cart", "cart-totals")):
        store.append("CycleClosed", {"component": component, "requirement": requirement},
                     stream=f"cycle:{component}/{requirement}")
    store.append("CycleDropped", {"component": "cart", "requirement": "cart-empties", "reason": "r"},
                 stream="cycle:cart/cart-empties")
    assert unproved(Ctx(store, repo, "it", None)) == ["store/store-refuses-negatives"]


def test_the_branch_autosquash_folds_a_fixup_of_the_model_s_commit(repo):
    # the session of 2026-11-15: martin fixed the static findings of an interface at the close, a fixup of the
    # model's commit, which is the components' base: the autosquash started from it and left the fixup unfolded
    from beck.machine.closing import handle_autosquash_branch
    model = git(repo, "rev-parse", "HEAD")
    subject = git(repo, "log", "-1", "--format=%s")
    store = Project(repo).start_iteration()[1]
    store.append("ModelCommitted", {"sha": model})
    store.append("ComponentStarted", {"component": "store", "worktree": str(repo), "branch": "b", "base": model},
                 stream="component:store")
    (repo / "src/shop/store/fixed.py").write_text("X = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"fixup! {subject}")
    command = store.append("CommandIssued", {"command": "AutosquashBranch", "target": "beck",
                                             "args": {"then": "review"}}, stream="branch")
    handle_autosquash_branch(command, Ctx(store, repo, "it", None))
    assert git(repo, "log", "-1", "--format=%s") == subject
    assert (repo / "src/shop/store/fixed.py").is_file()


def test_a_static_fix_of_a_line_older_than_the_branch_fixes_up_the_model_s_commit(repo):
    # the session of 2026-11-16: martin fixed pyproject.toml at the close; git blame named the scaffold's commit,
    # older than the branch, and the fixup of it could never fold
    from beck.machine.closing import handle_branch_fixes
    (repo / "pyproject.toml").write_text((repo / "pyproject.toml").read_text() + "# scaffold\n")
    git(repo, "commit", "-qam", "chore: scaffold the project")
    (repo / "model.txt").write_text("the model\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feat: the interfaces")
    model = git(repo, "rev-parse", "HEAD")
    store = Project(repo).start_iteration()[1]
    store.append("ModelCommitted", {"sha": model})
    store.append("ComponentStarted", {"component": "store", "worktree": str(repo), "branch": "b", "base": model},
                 stream="component:store")
    (repo / "pyproject.toml").write_text((repo / "pyproject.toml").read_text().replace("# scaffold", "# fixed"))
    command = store.append("CommandIssued", {"command": "CommitBranchFixes", "target": "beck", "args": {}},
                           stream="branch")
    handle_branch_fixes(command, Ctx(store, repo, "it", None))
    assert git(repo, "log", "-1", "--format=%s") == "fixup! feat: the interfaces"
