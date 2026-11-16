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
                "result": {"subject": "store.add returns the new total",
                           "summary": "store.add returns the new total. The cart reads it instead of adding again.",
                           "changes": "contract.md", "units": ["store", "cart"]}}


def plan(exclusive=False, waits=False):
    planned = {"units": [
        {"id": "store", "groups": [{"id": "entity:Store", "clauses": ["store-adds"]}], "depends_on": [],
         "implements": ["store-adds"], "exclusive_with": ["cart"] if exclusive else []},
        {"id": "cart", "groups": [{"id": "operation:totals", "clauses": ["cart-totals"]},
                                  {"id": "operation:empties", "clauses": ["cart-empties"]}],
         "depends_on": ["store"], "implements": ["cart-totals", "cart-empties"],
         "exclusive_with": ["store"] if exclusive else []},
    ]}
    if waits:   # cart's tests use the real store (boundary: concrete)
        planned["units"][1]["waits_for"] = ["store"]
    return planned


def setup(repo, agents, slots=2, exclusive=False, waits=False, replan=None):
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace(store, repo, lambda unit, tree: [f"src/shop/{unit}"],
                          lambda unit, group: {"unit": {"id": unit}, "group": group, "clauses": [], "entities": [],
                                               "dependencies": [], "files": {"source": [], "tests": []}},
                          replan_of=replan or (lambda: None))
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


def change_of(kind, target):
    def step(agents, envelope, cwd):
        return {"protocol": 1, "command": envelope["command"], "outcome": "change_requested",
                "change": {"kind": kind, "target": target, "problem": "p", "proposal": "q"}}
    return step


def test_a_change_of_a_test_stops_only_its_unit(repo):
    # the replay of 2026-11-16: martin asked to change a test of randomness, and the four units using it stopped
    from beck.machine.unit import _affected
    reactor, _ = setup(repo, Agents(repo, {}))
    assert _affected(reactor.ctx, "store", "store", "test") == ["store"]
    assert _affected(reactor.ctx, "store", "store", "interface") == ["store", "cart"]


def test_a_change_aimed_at_a_file_finds_the_unit_that_holds_it(repo):
    from beck.machine.unit import _owner
    reactor, _ = setup(repo, Agents(repo, {}))
    reactor.ctx.workspace.scope = lambda unit: [f"src/shop/{unit}"]   # as the index gives it, before any worktree
    assert _owner(reactor.ctx, "src/shop/store/store.py") == "store"
    assert _owner(reactor.ctx, "SystemRandomness.chance") is None


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
    assert approval.payload["args"]["proposal"]["subject"] == "store.add returns the new total"
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
    assert red_turns[-1]["retry"]["reason"] == ("resumed after a resolution: store.add returns the new total. "
                                                "The cart reads it instead of adding again.")


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


def without(clause):
    """The plan after a resolution that took `clause` out of the domain."""
    planned = plan()
    for u in planned["units"]:
        u["groups"] = [g for g in ({**g, "clauses": [c for c in g["clauses"] if c != clause]} for g in u["groups"])
                       if g["clauses"]]
        u["implements"] = [c for c in u["implements"] if c != clause]
    return planned


def approve(reactor):
    [approval] = pending_of_user(reactor)
    reactor.answer(approval.id, [Result("ResolutionApproved", {}, actor="user")])
    run(reactor)


def test_a_resolution_that_drops_a_clause_plans_the_units_again(repo):
    # the session of 2026-11-15: kent took a clause out of the domain, but the unit's plan still held it
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents, replan=lambda: without("cart-empties"))
    run(reactor)
    approve(reactor)
    assert len(list(store.read(types_=("UnitsPlanned",)))) == 2
    replanned = list(store.read(types_=("UnitsPlanned",)))[-1]
    assert not [e for e in store.read(types_=("CycleOpened", "CycleReopened"))
                if e.seq > replanned.seq and e.payload["clause"] == "cart-empties"]
    assert unit_events(store, "cart")[-1] == "UnitImplemented"


def test_the_open_cycle_of_a_dropped_clause_is_dropped_on_resume(repo):
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents, replan=lambda: without("store-adds"))
    run(reactor)
    approve(reactor)
    [dropped] = store.read(types_=("CycleDropped",))
    assert dropped.payload["unit"] == "store" and dropped.payload["clause"] == "store-adds"
    assert unit_events(store, "store")[-1] == "UnitImplemented"
    red_turns = [e for a, e in agents.calls if e["command"] == "WriteRedTest" and e["context"]["clause"] == "store-adds"]
    assert len(red_turns) == 1   # never asked again


def test_a_unit_parked_before_it_started_runs_its_cycles_once_started(repo):
    # the session of 2026-11-15: @integration, parked as a dependent while still waiting, started at last and
    # opened its first cycle, but the cycle took it for parked and asked unclebob for nothing
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents, slots=1)
    run(reactor)
    approve(reactor)
    assert unit_events(store, "cart") == ["UnitParked"]
    store.append("UnitClosed", {"unit": "store"}, stream="unit:store")
    run(reactor)
    assert [e["context"]["clause"] for a, e in agents.calls if e["command"] == "WriteRedTest"][-1] == "cart-empties"
    assert unit_events(store, "cart")[-1] == "UnitImplemented"


def test_a_dropped_cycle_takes_its_red_test_away(repo):
    # the session of 2026-11-15: the dropped clause's red test stayed committed, failed the unit's tests, and
    # the regression reopened the cycle of a clause no longer in the domain
    agents = Agents(repo, {("WriteGreenCode", "store-adds"): [change("store"), None]})
    reactor, store = setup(repo, agents, slots=1, replan=lambda: without("store-adds"))
    run(reactor)
    approve(reactor)
    tree = repo / ".beck/worktrees/store"
    assert not (tree / "tests/test_store_adds.py").exists()
    assert "test(store): store-adds" not in git(tree, "log", "--format=%s")
    assert not list(store.read(types_=("CycleReopened",)))
    assert unit_events(store, "store")[-1] == "UnitImplemented"


def test_a_green_that_keeps_asking_for_a_change_goes_back_to_red(repo):
    # the session of 2026-11-15: unclebob asked ten times, from the same green, for a change kent did not find,
    # since the committed red test was wrong; at the repetition rule's times beck takes the cycle back to red
    agents = Agents(repo, {("WriteGreenCode", "store-adds"): [change("store")] * 3 + [None]})
    reactor, store = setup(repo, agents, slots=1)
    run(reactor)
    for _ in range(2):
        approve(reactor)
    assert [e.payload["clause"] for e in store.read(types_=("RedRewound",))] == ["store-adds"]
    assert len(list(store.read(types_=("ResolutionProposed",)))) == 2   # the third request is not resolved
    assert unit_events(store, "store")[-1] == "UnitImplemented"


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


def test_a_malformed_answer_outside_the_cycles_is_asked_again(repo):
    # the session of 2026-11-16: kent answered a ProposeResolution with its result under `change`; beck rejected
    # it and nothing asked again: the session stopped with nothing to do and nothing to answer
    def malformed(agents, envelope, cwd):
        return {"protocol": 1, "command": "ProposeResolution", "outcome": "done",
                "change": {"subject": "s", "units": []}}
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None],
                           ("ProposeResolution", "interface"): [malformed, None]})
    reactor, store = setup(repo, agents)
    run(reactor)
    assert len(list(store.read(types_=("SignalRejected",)))) == 1
    proposals = [e for a, e in agents.calls if e["command"] == "ProposeResolution"]
    assert len(proposals) == 2 and "change: 'kind' is a required property" in proposals[1]["retry"]["reason"]
    assert [c.payload["command"] for c in pending_of_user(reactor)] == ["ApproveResolution"]


def test_a_stale_rejection_is_not_asked_again(repo):
    # the session of 2026-11-16: on attach, the retry of malformed answers picked up martin's old rejections, on
    # (ExplainUnit: a turn of a unit with no retry of its own; martin's are retried by beck.machine.refactor)
    # units closed long before, whose worktrees were gone: six turns crashed
    from beck.machine.core import retry_a_rejected_turn
    reactor, store = setup(repo, Agents(repo))
    run(reactor)
    turn = store.append("CommandIssued", {"command": "ProposeResolution", "target": "kent",
                                          "args": {"session": "s", "context": {"kind": "interface", "details": {}}}},
                        stream="resolution")
    rejected = store.append("SignalRejected", {"agent": "kent", "command": "ProposeResolution", "errors": ["e"],
                                               "signature": "x"}, stream="resolution", cause=turn)
    assert retry_a_rejected_turn(rejected, reactor.ctx)                        # the latest: asked again
    store.append("CommandIssued", {"command": "ProposeResolution", "target": "kent",
                                   "args": {"session": "s", "context": {"kind": "interface", "details": {}}}},
                 stream="resolution")
    assert retry_a_rejected_turn(rejected, reactor.ctx) == []                  # a later turn: stale
    cart = store.append("CommandIssued", {"command": "ExplainUnit", "target": "martin",
                                          "args": {"session": "m", "unit": "cart", "context": {}}}, stream="unit:cart")
    open_ = store.append("SignalRejected", {"agent": "martin", "command": "ExplainUnit", "errors": ["e"],
                                            "signature": "z"}, stream="unit:cart", cause=cart)
    assert retry_a_rejected_turn(open_, reactor.ctx)                           # its unit still open: asked again
    closed = store.append("CommandIssued", {"command": "ExplainUnit", "target": "martin",
                                            "args": {"session": "m", "unit": "store", "context": {}}},
                          stream="unit:store")
    store.append("UnitClosed", {"unit": "store"}, stream="unit:store")
    old = store.append("SignalRejected", {"agent": "martin", "command": "ExplainUnit", "errors": ["e"],
                                          "signature": "y"}, stream="unit:store", cause=closed)
    assert retry_a_rejected_turn(old, reactor.ctx) == []                       # its unit closed


def test_a_rejected_resolution_brings_back_a_file_kent_removed(repo):
    # the session of 2026-11-16: no agent could delete a file, so the user did it by hand; kent and martin may
    # git rm now, and a rejected resolution must bring the file back
    from beck.drivers.claude import allowed_tools
    from beck.machine.unit import discard_resolution
    assert "Bash(git rm:*)" in allowed_tools("kent") and "Bash(git rm:*)" in allowed_tools("martin")
    assert "Bash(git rm:*)" not in allowed_tools("unclebob")
    git(repo, "rm", "-q", "src/shop/store/store_adds.py")
    reactor, store = setup(repo, Agents(repo))
    discard_resolution(None, reactor.ctx)
    assert (repo / "src/shop/store/store_adds.py").is_file() and git(repo, "status", "--short") == ""
