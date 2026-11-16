"""The flakiness detector on a real repository: a test whose results change on the same tree is hunted."""

import asyncio

from test_cycle import git, repo  # noqa: F401

from beck.events.project import Project
from beck.machine import user
from beck.machine.core import Ctx, Reactor
from beck.machine.cycle import Workspace
from beck.machine.flaky import flaky
from beck.testrun import run as run_tests
from beck.toolchain import load

TEST = "tests/test_flaky.py::test_sometimes"
STEADY = "tests/test_flaky.py::test_always"


def scheduled(repo, schedule):
    """A test that passes or fails by a schedule kept outside the tree: P passes, F fails, one letter a run."""
    state = repo.parent / f"{repo.name}-schedule"
    state.write_text(schedule)
    (repo / "tests/test_flaky.py").write_text(
        "from pathlib import Path\n\nSTATE = Path(%r)\n\n\n"
        "def test_sometimes():\n    s = STATE.read_text()\n    STATE.write_text(s[1:] or s[-1])\n"
        "    assert s[0] == 'P'\n\n\ndef test_always():\n    assert True\n" % str(state))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a flaky test")


def start(repo, runs=2):
    project = Project(repo)
    it, store, _ = project.start_iteration()
    for _ in range(runs):   # the agent's runs and beck's verification, on the same tree
        run_tests(repo, load(repo), [TEST, STEADY], actor="unclebob", component="u", iteration=it.id)
    ctx = Ctx(store, repo, it.id, Workspace({"u": repo}, {"u": ["src/pkg"]}, {}), flakiness=(3, 4))
    return Reactor(ctx, flaky, None), store


def test_mixed_results_on_one_tree_are_hunted_alone_and_found(repo):
    scheduled(repo, "PF" + "PFPP")
    reactor, store = start(repo)
    asyncio.run(reactor.run_until_idle())
    [found] = store.read(types_=("FlakyTestFound",))
    assert found.payload == {"test": TEST, "stage": "alone", "component": "u",
                             "evidence": "alone: 3 of 4 runs passed, on one tree"}
    hunts = [e.payload["args"]["test"] for e in store.read(types_=("CommandIssued",))]
    assert hunts == [TEST]                       # the steady test is not hunted, nor the hunt's own runs
    assert not (repo / ".beck/worktrees").exists() or not any((repo / ".beck/worktrees").iterdir())


def test_steady_results_escalate_to_the_component_then_ask_for_the_suite(repo):
    scheduled(repo, "PF" + "PPPP" + "PPPP" + "PPFP")
    reactor, store = start(repo)
    store.append("RedVerified", {"component": "u", "requirement": "c", "test": TEST, "run": "r"})
    asyncio.run(reactor.run_until_idle())
    assert [e.payload["stage"] for e in store.read(types_=("FlakinessUnsure",))] == ["alone", "component"]
    [ask] = [c for c in reactor.pending() if c.payload["target"] == "user"]
    assert ask.payload["command"] == "ApproveSuiteRuns" and "run the whole suite 4 times" in user.prompt(ask)
    reactor.answer(ask.id, user.answer(ask, "yes"))
    asyncio.run(reactor.run_until_idle())
    [found] = store.read(types_=("FlakyTestFound",))
    assert found.payload["stage"] == "suite"


def test_a_refused_suite_leaves_it_unresolved(repo):
    scheduled(repo, "PF" + "P")
    reactor, store = start(repo)
    asyncio.run(reactor.run_until_idle())
    assert [e.payload["stage"] for e in store.read(types_=("FlakinessUnsure",))] == ["alone"]
    [ask] = [c for c in reactor.pending() if c.payload["target"] == "user"]   # no component tests: straight to the user
    reactor.answer(ask.id, user.answer(ask, "no"))
    asyncio.run(reactor.run_until_idle())
    assert [e.type for e in store.read(types_=("FlakinessUnresolved", "FlakyTestFound"))] == ["FlakinessUnresolved"]


def test_a_failure_after_a_change_is_no_flakiness(repo):
    scheduled(repo, "P")
    reactor, store = start(repo, runs=1)
    (repo / "tests/test_flaky.py").write_text((repo / "tests/test_flaky.py").read_text().replace("== 'P'", "== 'X'"))
    run_tests(repo, load(repo), [TEST], actor="unclebob", component="u", iteration=reactor.ctx.iteration)
    asyncio.run(reactor.run_until_idle())
    assert not [e for e in store.read(types_=("CommandIssued",))]
