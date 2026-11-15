"""The units' machine on a real repository: worktrees, groups, the scheduler, parking and resolutions."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from beck.events.project import Project
from beck.machine.core import Ctx, Reactor, Result, Turn
from beck.machine.unit import Workspace, implementation
from beck.testrun import run as run_tests
from beck.toolchain import load

PYTEST = f'[{sys.executable}, -m, pytest, -q, -p, no:cacheprovider, "--junitxml={{report}}", "{{tests}}"]'
CLAUSES = {"store": ["store-adds"], "cart": ["cart-totals", "cart-empties"]}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def fn(clause):
    return clause.replace("-", "_")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    for unit, clauses in CLAUSES.items():
        pkg = tmp_path / "src/shop" / unit
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").touch()
        for c in clauses:  # the interfaces: a stub per operation
            (pkg / f"{fn(c)}.py").write_text(f"def {fn(c)}():\n    raise NotImplementedError\n")
    (tmp_path / "src/shop/__init__.py").touch()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/.gitkeep").touch()
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck/toolchain.yml").write_text(f"toolchain: 1\nlanguage: python\ntest: {PYTEST}\ncheck: []\n")
    git(tmp_path, "init", "-q", "-b", "feature")
    git(tmp_path, "config", "user.email", "beck@example.com")
    git(tmp_path, "config", "user.name", "beck")
    Project(tmp_path)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "interfaces")
    for var in ("BECK_CAUSE", "BECK_ITERATION", "BECK_ACTOR", "BECK_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class Agents:
    """unclebob writes a test and then the code of each clause; kent proposes a resolution.

    `script` overrides a (command, key) with one step per call, the last repeating; key is the clause
    for unclebob, the kind for kent."""

    def __init__(self, project, script=None):
        self.project, self.script, self.calls = project, script or {}, []

    async def turn(self, agent, session, envelope, env, cwd):
        self.calls.append((agent, envelope))
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return Turn(self._play(agent, envelope, Path(cwd)), "fake", {"input": 1, "output": 1})
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def _play(self, agent, envelope, cwd):
        command, context = envelope["command"], envelope["context"]
        key = context.get("clause") or context.get("kind") or "answer"
        steps = self.script.get((command, key))
        if steps:
            n = sum(1 for a, e in self.calls if e["command"] == command and
                    (e["context"].get("clause") or e["context"].get("kind") or "answer") == key)
            step = steps[min(n, len(steps)) - 1]
            if step is not None:
                return step(self, envelope, cwd)
        return getattr(self, command)(envelope, cwd)

    def test(self, cwd, unit, clause):
        run_tests(cwd, load(self.project), [f"tests/test_{fn(clause)}.py::test_{fn(clause)}"], actor="unclebob",
                  unit=unit, clause=clause, store_root=self.project)

    def WriteRedTest(self, envelope, cwd):
        clause = envelope["context"]["clause"]
        unit = next(u for u, cs in CLAUSES.items() if clause in cs)
        (cwd / f"tests/test_{fn(clause)}.py").write_text(
            f"from shop.{unit}.{fn(clause)} import {fn(clause)}\n\n\n"
            f"def test_{fn(clause)}():\n    # spec: {clause}\n    assert {fn(clause)}() == 1\n")
        self.test(cwd, unit, clause)
        return {"protocol": 1, "command": "WriteRedTest", "outcome": "done",
                "result": {"test": f"tests/test_{fn(clause)}.py::test_{fn(clause)}"}}

    def WriteGreenCode(self, envelope, cwd):
        clause = envelope["context"]["clause"]
        unit = next(u for u, cs in CLAUSES.items() if clause in cs)
        (cwd / f"src/shop/{unit}/{fn(clause)}.py").write_text(f"def {fn(clause)}():\n    return 1\n")
        self.test(cwd, unit, clause)
        return {"protocol": 1, "command": "WriteGreenCode", "outcome": "done"}

    def ProposeResolution(self, envelope, cwd):
        (cwd / "src/shop/store/contract.md").write_text("store: add returns the new total\n")
        return {"protocol": 1, "command": "ProposeResolution", "outcome": "done",
                "result": {"summary": "store.add returns the new total", "changes": "contract.md",
                           "units": ["store", "cart"]}}


def plan(exclusive=False, waits=False):
    planned = {"units": [
        {"id": "store", "groups": [{"id": "entity:Store", "clauses": ["store-adds"]}], "depends_on": [],
         "realizes": ["store-adds"], "exclusive_with": ["cart"] if exclusive else []},
        {"id": "cart", "groups": [{"id": "operation:totals", "clauses": ["cart-totals"]},
                                  {"id": "operation:empties", "clauses": ["cart-empties"]}],
         "depends_on": ["store"], "realizes": ["cart-totals", "cart-empties"],
         "exclusive_with": ["store"] if exclusive else []},
    ]}
    if waits:   # cart's tests use the real store (boundary: concrete)
        planned["units"][1]["waits_for"] = ["store"]
    return planned


def setup(repo, agents, slots=2, exclusive=False, waits=False):
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace(store, repo, lambda unit, tree: [f"src/shop/{unit}"],
                          lambda unit, group: {"unit": {"id": unit}, "group": group, "clauses": [], "entities": [],
                                               "dependencies": [], "files": {"source": [], "tests": []}})
    ctx = Ctx(store, repo, it.id, workspace, units_in_parallel=slots)
    store.append("UnitsPlanned", plan(exclusive, waits))
    return Reactor(ctx, implementation, agents), store


def types(store, *only):
    return [e.type for e in store.read() if e.type in only]


def unit_events(store, unit):
    return [e.type for e in store.read(stream=f"unit:{unit}")
            if e.type not in ("CommandIssued", "CommandDone", "AgentTurnEnded", "TestsRun", "ChecksRun")]


def run(reactor):
    asyncio.run(reactor.run_until_idle())


def test_two_units_run_in_parallel_through_their_groups(repo):
    reactor, store = setup(repo, Agents(repo))
    run(reactor)
    assert unit_events(store, "cart") == ["UnitStarted", "GroupOpened", "GroupClosed", "GroupOpened", "GroupClosed",
                                          "UnitImplemented"]
    assert unit_events(store, "store")[-1] == "UnitImplemented"
    cart_tree = repo / ".beck/worktrees/cart"
    assert git(cart_tree, "log", "--format=%s").splitlines() == [
        "feat(cart): cart-empties", "test(cart): cart-empties", "feat(cart): cart-totals", "test(cart): cart-totals",
        "interfaces"]
    assert git(repo, "log", "--format=%s") == "interfaces"   # the feature branch waits for the landings


def test_the_slots_limit_the_units_running(repo):
    reactor, store = setup(repo, Agents(repo), slots=1)
    run(reactor)
    assert unit_events(store, "store")[-1] == "UnitImplemented" and unit_events(store, "cart") == []


def test_units_sharing_a_file_never_run_together(repo):
    reactor, store = setup(repo, Agents(repo), slots=2, exclusive=True)
    run(reactor)
    assert unit_events(store, "cart") == []


def test_a_unit_waits_for_its_concrete_dependencies_to_land(repo):
    # the session of 2026-11-15: match-program, whose dependencies are concrete, started beside them and tested
    # against their stubs (NotImplementedError); unclebob asked for an architecture change three times
    reactor, store = setup(repo, Agents(repo), slots=2, waits=True)
    run(reactor)
    assert unit_events(store, "store")[-1] == "UnitImplemented" and unit_events(store, "cart") == []
    store.append("UnitClosed", {"unit": "store"}, stream="unit:store")
    run(reactor)
    assert unit_events(store, "cart")[0] == "UnitStarted"


def change(target):
    def step(agents, envelope, cwd):
        return {"protocol": 1, "command": envelope["command"], "outcome": "change_requested",
                "change": {"kind": "interface", "target": target, "problem": "add returns nothing",
                           "proposal": "return the new total"}}
    return step


def pending_of_user(reactor):
    return [c for c in reactor.pending() if c.payload["target"] == "user"]


def test_a_change_parks_the_unit_and_its_dependents_until_the_user_approves(repo):
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents)
    run(reactor)
    parked = [e.payload["unit"] for e in store.read(types_=("UnitParked",))]
    assert sorted(parked) == ["cart", "store"]
    [approval] = pending_of_user(reactor)
    assert approval.payload["command"] == "ApproveResolution"
    assert approval.payload["args"]["proposal"]["summary"] == "store.add returns the new total"
    assert (repo / "src/shop/store/contract.md").exists() and "contract.md" in git(repo, "status", "--short")

    reactor.answer(approval.id, [Result("ResolutionApproved", {}, actor="user")])
    run(reactor)
    assert git(repo, "log", "-1", "--format=%s") == "refactor: store.add returns the new total"
    resolution = git(repo, "rev-parse", "HEAD")
    for unit in ("store", "cart"):
        tree = repo / f".beck/worktrees/{unit}"
        assert subprocess.run(["git", "merge-base", "--is-ancestor", resolution, "HEAD"], cwd=tree).returncode == 0
        assert unit_events(store, unit)[-1] == "UnitImplemented"
    reopened = list(store.read(types_=("CycleReopened",)))
    assert reopened and all(e.payload["reason"].startswith("resumed after a resolution: store.add") for e in reopened)
    red_turns = [e for a, e in agents.calls if e["command"] == "WriteRedTest" and e["context"]["clause"] == "store-adds"]
    assert red_turns[-1]["retry"]["reason"] == "resumed after a resolution: store.add returns the new total"


def test_a_waiting_unit_parked_with_its_dependency_is_not_started(repo):
    # the session of 2026-11-15: ParkUnits parked eight units at once, and the scheduler, reading the state as
    # at each UnitParked, started the later ones (still waiting at that event), each of them up to three times
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents, slots=1)
    run(reactor)
    assert unit_events(store, "cart") == ["UnitParked"]
    assert [e.payload["args"]["unit"] for e in store.read(types_=("CommandIssued",))
            if e.payload["command"] == "StartUnit"] == ["store"]
    [approval] = pending_of_user(reactor)
    reactor.answer(approval.id, [Result("ResolutionApproved", {}, actor="user")])
    run(reactor)
    assert unit_events(store, "store")[-1] == "UnitImplemented"   # cart waits for its slot: store has not landed
    starts = [e.payload["args"]["unit"] for e in store.read(types_=("CommandIssued",))
              if e.payload["command"] in ("StartUnit", "ResumeUnit")]
    assert starts == ["store", "store"] and unit_events(store, "cart") == ["UnitParked"]


def test_a_rejected_resolution_is_discarded_and_proposed_again(repo):
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents)
    run(reactor)
    [approval] = pending_of_user(reactor)
    reactor.answer(approval.id, [Result("ResolutionRejected", {"instructions": "keep add, add total"}, actor="user")])
    run(reactor)
    proposals = [e for a, e in agents.calls if e["command"] == "ProposeResolution"]
    assert proposals[-1]["context"]["instructions"] == "keep add, add total"
    assert len(pending_of_user(reactor)) == 1      # the new proposal waits for the user
    assert git(repo, "log", "--format=%s") == "interfaces"


def test_kent_asks_and_the_wrapped_answer_goes_back_to_its_session(repo):
    ask = lambda agents, envelope, cwd: {"protocol": 1, "command": "ProposeResolution", "outcome": "question",  # noqa
                                          "question": {"text": "Return the total or the cart?", "why": "Both fit."}}
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None],
                           ("ProposeResolution", "interface"): [ask, None]})
    reactor, store = setup(repo, agents)
    run(reactor)
    [question] = pending_of_user(reactor)
    assert question.payload["command"] == "Ask"
    reactor.answer(question.id, [Result("QuestionAnswered", {"text": "The total."}, actor="user")])
    run(reactor)
    kent = [e for a, e in agents.calls if a == "kent"]
    assert kent[-1]["answer"] == {"question": {"text": "Return the total or the cart?", "why": "Both fit."},
                                  "text": "The total."} and kent[-1]["context"] == {}
    assert [c.payload["command"] for c in pending_of_user(reactor)] == ["ApproveResolution"]


def test_a_blocked_cycle_parks_its_unit_and_asks_kent(repo):
    passing = lambda agents, envelope, cwd: (  # noqa: E731
        (cwd / "tests/test_store_adds.py").write_text("def test_store_adds():\n    assert True\n"),
        agents.test(cwd, "store", "store-adds"),
        {"protocol": 1, "command": "WriteRedTest", "outcome": "done",
         "result": {"test": "tests/test_store_adds.py::test_store_adds"}})[-1]
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [passing]})
    reactor, store = setup(repo, agents)
    run(reactor)
    assert types(store, "CycleBlocked") == ["CycleBlocked"]
    assert [e.payload["unit"] for e in store.read(types_=("UnitParked",))] == ["store"]
    kent = [e for a, e in agents.calls if a == "kent"]
    assert kent[0]["context"]["kind"] == "blocked_cycle"
    assert unit_events(store, "cart")[-1] == "UnitImplemented"   # the rest goes on
