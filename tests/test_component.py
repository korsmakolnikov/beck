"""The components' machine on a real repository: worktrees, units, the scheduler, parking and restarts."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agent_tools import send_test

from beck.events.project import Project
from beck.machine.component import Workspace, implementation
from beck.machine.core import Ctx, Engine, Result, Turn

PYTEST = f'[{sys.executable}, -m, pytest, -q, -p, no:cacheprovider, "--junitxml={{report}}", "{{tests}}"]'
REQUIREMENTS = {"store": ["store-adds"], "cart": ["cart-totals", "cart-empties"]}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def fn(requirement):
    return requirement.replace("-", "_")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    for component, requirements in REQUIREMENTS.items():
        pkg = tmp_path / "src/shop" / component
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").touch()
        for c in requirements:  # the interfaces: an unimplemented per morphism
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
    """unclebob writes a test and then the code of each requirement; kent proposes a restart.

    `script` overrides a (command, key) with one step per call, the last repeating; key is the requirement
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
        key = context.get("requirement") or context.get("kind") or "answer"
        steps = self.script.get((command, key))
        if steps:
            n = sum(1 for a, e in self.calls if e["command"] == command and
                    (e["context"].get("requirement") or e["context"].get("kind") or "answer") == key)
            step = steps[min(n, len(steps)) - 1]
            if step is not None:
                return step(self, envelope, cwd)
        return getattr(self, command)(envelope, cwd)

    def test(self, cwd, component, requirement):
        send_test(cwd, [f"tests/test_{fn(requirement)}.py::test_{fn(requirement)}"], component=component,
                  requirement=requirement)

    def WriteRedTest(self, envelope, cwd):
        requirement = envelope["context"]["requirement"]
        component = next(u for u, cs in REQUIREMENTS.items() if requirement in cs)
        (cwd / f"tests/test_{fn(requirement)}.py").write_text(
            f"from shop.{component}.{fn(requirement)} import {fn(requirement)}\n\n\n"
            f"def test_{fn(requirement)}():\n    # spec: {requirement}\n    assert {fn(requirement)}() == 1\n")
        self.test(cwd, component, requirement)
        return {"protocol": 1, "command": "WriteRedTest", "outcome": "done",
                "result": {"test": f"tests/test_{fn(requirement)}.py::test_{fn(requirement)}"}}

    def WriteGreenCode(self, envelope, cwd):
        requirement = envelope["context"]["requirement"]
        component = next(u for u, cs in REQUIREMENTS.items() if requirement in cs)
        (cwd / f"src/shop/{component}/{fn(requirement)}.py").write_text(f"def {fn(requirement)}():\n    return 1\n")
        self.test(cwd, component, requirement)
        return {"protocol": 1, "command": "WriteGreenCode", "outcome": "done"}

    def ProposeRestart(self, envelope, cwd):
        (cwd / "src/shop/store/contract.md").write_text("store: add returns the new total\n")
        return {"protocol": 1, "command": "ProposeRestart", "outcome": "done",
                "result": {"subject": "store.add returns the new total",
                           "summary": "store.add returns the new total. The cart reads it instead of adding again.",
                           "changes": "contract.md", "components": ["store", "cart"]}}


def execution(exclusive=False, waits=False):
    scheduled = {"components": [
        {"id": "store", "units": [{"id": "model:Store", "requirements": ["store-adds"]}], "depends_on": [],
         "implements": ["store-adds"], "exclusive_with": ["cart"] if exclusive else []},
        {"id": "cart", "units": [{"id": "morphism:totals", "requirements": ["cart-totals"]},
                                  {"id": "morphism:empties", "requirements": ["cart-empties"]}],
         "depends_on": ["store"], "implements": ["cart-totals", "cart-empties"],
         "exclusive_with": ["store"] if exclusive else []},
    ]}
    if waits:   # cart's tests use the real store (boundary: concrete)
        scheduled["components"][1]["waits_for"] = ["store"]
    return scheduled


def setup(repo, agents, slots=2, exclusive=False, waits=False, rederive=None, initial=None):
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace(store, repo, lambda component, tree: [f"src/shop/{component}"],
                          lambda component, unit: {"component": {"id": component}, "unit": unit, "requirements": [],
                                                   "entities": [],
                                               "dependencies": [], "files": {"source": [], "tests": []}},
                          rederive_of=rederive or (lambda: None))
    ctx = Ctx(store, repo, it.id, workspace, components_in_parallel=slots)
    engine = Engine(ctx, implementation, agents)
    engine.record("ExecutionDerived", initial or execution(exclusive, waits))
    return engine, store


def types(store, *only):
    return [e.type for e in store.read() if e.type in only]


def component_events(store, component):
    return [e.type for e in store.read(stream=f"component:{component}")
            if e.type not in ("AgentTurnRequested", "DecisionRequested", "AgentTurnStarted", "AgentTurnEnded", "TestsRun",
                              "ChecksRun")]


def run(engine):
    asyncio.run(engine.run_until_idle())


def test_two_components_run_in_parallel_through_their_units(repo):
    engine, store = setup(repo, Agents(repo))
    run(engine)
    assert component_events(store, "cart") == ["ComponentStarted", "UnitOpened", "UnitClosed", "UnitOpened", "UnitClosed",
                                          "ComponentImplemented"]
    assert component_events(store, "store")[-1] == "ComponentImplemented"
    cart_tree = repo / ".beck/worktrees/cart"
    assert git(cart_tree, "log", "--format=%s").splitlines() == [
        "feat(cart): cart-empties", "test(cart): cart-empties", "feat(cart): cart-totals", "test(cart): cart-totals",
        "interfaces"]
    assert git(repo, "log", "--format=%s") == "interfaces"   # the feature branch waits for the landings


def test_the_slots_limit_the_components_running(repo):
    engine, store = setup(repo, Agents(repo), slots=1)
    run(engine)
    assert component_events(store, "store")[-1] == "ComponentImplemented" and component_events(store, "cart") == []


def test_components_sharing_a_file_never_run_together(repo):
    engine, store = setup(repo, Agents(repo), slots=2, exclusive=True)
    run(engine)
    assert component_events(store, "cart") == []


def test_a_component_waits_for_its_concrete_dependencies_to_land(repo):
    # the session of 2026-11-15: match-program, whose dependencies are concrete, started beside them and tested
    # against their unimplemented (NotImplementedError); unclebob asked for an architecture change three times
    engine, store = setup(repo, Agents(repo), slots=2, waits=True)
    run(engine)
    assert component_events(store, "store")[-1] == "ComponentImplemented" and component_events(store, "cart") == []
    engine.record("ComponentClosed", {"component": "store"}, stream="component:store")
    run(engine)
    assert component_events(store, "cart")[0] == "ComponentStarted"


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


def test_a_change_of_a_test_stops_only_its_component(repo):
    # the replay of 2026-11-16: martin asked to change a test of randomness, and the four components using it stopped
    from beck.machine.component import _affected
    engine, _ = setup(repo, Agents(repo, {}))
    assert _affected(engine.ctx, "store", "store", "test") == ["store"]
    assert _affected(engine.ctx, "store", "store", "interface") == ["store", "cart"]


def test_a_change_aimed_at_a_file_finds_the_component_that_holds_it(repo):
    from beck.machine.component import _owner
    engine, _ = setup(repo, Agents(repo, {}))
    engine.ctx.workspace.scope = lambda component: [f"src/shop/{component}"]   # as the index gives it, before any worktree
    assert _owner(engine.ctx, "src/shop/store/store.py") == "store"
    assert _owner(engine.ctx, "SystemRandomness.chance") is None


def pending_of_user(engine):
    return [c for c in engine.pending() if c.payload["target"] == "user"]


def test_a_change_parks_the_component_and_its_dependents_until_the_user_approves(repo):
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents)
    run(engine)
    parked = [e.payload["component"] for e in store.read(types_=("ComponentParked",))]
    assert sorted(parked) == ["cart", "store"]
    [approval] = pending_of_user(engine)
    assert approval.payload["command"] == "ApproveRestart"
    assert approval.payload["args"]["proposal"]["subject"] == "store.add returns the new total"
    assert (repo / "src/shop/store/contract.md").exists() and "contract.md" in git(repo, "status", "--short")

    engine.answer(approval.id, [Result("RestartApproved", {}, actor="user")])
    run(engine)
    assert git(repo, "log", "-1", "--format=%s") == "refactor: store.add returns the new total"
    restart = git(repo, "rev-parse", "HEAD")
    for component in ("store", "cart"):
        tree = repo / f".beck/worktrees/{component}"
        assert subprocess.run(["git", "merge-base", "--is-ancestor", restart, "HEAD"], cwd=tree).returncode == 0
        assert component_events(store, component)[-1] == "ComponentImplemented"
    reopened = list(store.read(types_=("CycleReopened",)))
    assert reopened and all(e.payload["reason"].startswith("resumed after a restart: store.add") for e in reopened)
    red_turns = [e for a, e in agents.calls if e["command"] == "WriteRedTest" and e["context"]["requirement"] == "store-adds"]
    assert red_turns[-1]["retry"]["reason"] == ("resumed after a restart: store.add returns the new total. "
                                                "The cart reads it instead of adding again.")


def test_a_waiting_component_parked_with_its_dependency_is_not_started(repo):
    # the session of 2026-11-15: ParkComponents parked eight components at once, and the scheduler, reading the state as
    # at each ComponentParked, started the later ones (still waiting at that event), each of them up to three times
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents, slots=1)
    run(engine)
    assert component_events(store, "cart") == ["ComponentParked"]
    assert [e.payload["component"] for e in store.read(types_=("ComponentStarted",))] == ["store"]
    [approval] = pending_of_user(engine)
    engine.answer(approval.id, [Result("RestartApproved", {}, actor="user")])
    run(engine)
    assert component_events(store, "store")[-1] == "ComponentImplemented"   # cart waits for its slot: store has not landed
    starts = [e.payload["component"] for e in store.read(types_=("ComponentStarted", "ComponentResumed"))]
    assert starts == ["store", "store"] and component_events(store, "cart") == ["ComponentParked"]


def without(requirement):
    """The execution after a restart that took `requirement` out of the domain."""
    scheduled = execution()
    for u in scheduled["components"]:
        u["units"] = [g for g in ({**g, "requirements": [c for c in g["requirements"] if c != requirement]} for g in u["units"])
                       if g["requirements"]]
        u["implements"] = [c for c in u["implements"] if c != requirement]
    return scheduled


def approve(engine):
    [approval] = pending_of_user(engine)
    engine.answer(approval.id, [Result("RestartApproved", {}, actor="user")])
    run(engine)


def test_a_restart_that_drops_a_requirement_plans_the_components_again(repo):
    # the session of 2026-11-15: kent took a requirement out of the domain, but the component's execution still held it
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents, rederive=lambda: without("cart-empties"))
    run(engine)
    approve(engine)
    assert len(list(store.read(types_=("ExecutionDerived",)))) == 2
    rederived = list(store.read(types_=("ExecutionDerived",)))[-1]
    assert not [e for e in store.read(types_=("CycleOpened", "CycleReopened"))
                if e.seq > rederived.seq and e.payload["requirement"] == "cart-empties"]
    assert component_events(store, "cart")[-1] == "ComponentImplemented"


def test_the_open_cycle_of_a_dropped_requirement_is_dropped_on_resume(repo):
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents, rederive=lambda: without("store-adds"))
    run(engine)
    approve(engine)
    [dropped] = store.read(types_=("CycleDropped",))
    assert dropped.payload["component"] == "store" and dropped.payload["requirement"] == "store-adds"
    assert component_events(store, "store")[-1] == "ComponentImplemented"
    red_turns = [e for a, e in agents.calls if e["command"] == "WriteRedTest" and e["context"]["requirement"] == "store-adds"]
    assert len(red_turns) == 1   # never asked again


def test_a_component_parked_before_it_started_runs_its_cycles_once_started(repo):
    # the session of 2026-11-15: @integration, parked as a dependent while still waiting, started at last and
    # opened its first cycle, but the cycle took it for parked and asked unclebob for nothing
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents, slots=1)
    run(engine)
    approve(engine)
    assert component_events(store, "cart") == ["ComponentParked"]
    engine.record("ComponentClosed", {"component": "store"}, stream="component:store")
    run(engine)
    assert [e["context"]["requirement"] for a, e in agents.calls if e["command"] == "WriteRedTest"][-1] == "cart-empties"
    assert component_events(store, "cart")[-1] == "ComponentImplemented"


def test_a_dropped_cycle_takes_its_red_test_away(repo):
    # the session of 2026-11-15: the dropped requirement's red test stayed committed, failed the component's tests, and
    # the regression reopened the cycle of a requirement no longer in the domain
    agents = Agents(repo, {("WriteGreenCode", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents, slots=1, rederive=lambda: without("store-adds"))
    run(engine)
    approve(engine)
    tree = repo / ".beck/worktrees/store"
    assert not (tree / "tests/test_store_adds.py").exists()
    assert "test(store): store-adds" not in git(tree, "log", "--format=%s")
    assert not list(store.read(types_=("CycleReopened",)))
    assert component_events(store, "store")[-1] == "ComponentImplemented"


def test_a_green_that_keeps_asking_for_a_change_goes_back_to_red(repo):
    # the session of 2026-11-15: unclebob asked ten times, from the same green, for a change kent did not find,
    # since the committed red test was wrong; at the repetition rule's times beck takes the cycle back to red
    agents = Agents(repo, {("WriteGreenCode", "store-adds"): [change("store")] * 3 + [None]})
    engine, store = setup(repo, agents, slots=1)
    run(engine)
    for _ in range(2):
        approve(engine)
    assert [e.payload["requirement"] for e in store.read(types_=("RedRewound",))] == ["store-adds"]
    assert len(list(store.read(types_=("RestartProposed",)))) == 2   # the third request is not resolved
    assert component_events(store, "store")[-1] == "ComponentImplemented"


def test_a_rejected_restart_is_discarded_and_proposed_again(repo):
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None]})
    engine, store = setup(repo, agents)
    run(engine)
    [approval] = pending_of_user(engine)
    engine.answer(approval.id, [Result("RestartRejected", {"instructions": "keep add, add total"}, actor="user")])
    run(engine)
    proposals = [e for a, e in agents.calls if e["command"] == "ProposeRestart"]
    assert proposals[-1]["context"]["instructions"] == "keep add, add total"
    assert len(pending_of_user(engine)) == 1      # the new proposal waits for the user
    assert git(repo, "log", "--format=%s") == "interfaces"


def test_kent_asks_and_the_wrapped_answer_goes_back_to_its_session(repo):
    ask = lambda agents, envelope, cwd: {"protocol": 1, "command": "ProposeRestart", "outcome": "question",  # noqa
                                          "question": {"text": "Return the total or the cart?", "why": "Both fit."}}
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None],
                           ("ProposeRestart", "interface"): [ask, None]})
    engine, store = setup(repo, agents)
    run(engine)
    [question] = pending_of_user(engine)
    assert question.payload["command"] == "Ask"
    engine.answer(question.id, [Result("QuestionAnswered", {"text": "The total."}, actor="user")])
    run(engine)
    kent = [e for a, e in agents.calls if a == "kent"]
    assert kent[-1]["answer"] == {"question": {"text": "Return the total or the cart?", "why": "Both fit."},
                                  "text": "The total."} and kent[-1]["context"] == {}
    assert [c.payload["command"] for c in pending_of_user(engine)] == ["ApproveRestart"]


def test_a_blocked_cycle_parks_its_component_and_asks_kent(repo):
    passing = lambda agents, envelope, cwd: (  # noqa: E731
        (cwd / "tests/test_store_adds.py").write_text("def test_store_adds():\n    assert True\n"),
        agents.test(cwd, "store", "store-adds"),
        {"protocol": 1, "command": "WriteRedTest", "outcome": "done",
         "result": {"test": "tests/test_store_adds.py::test_store_adds"}})[-1]
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [passing]})
    engine, store = setup(repo, agents)
    run(engine)
    assert types(store, "CycleBlocked") == ["CycleBlocked"]
    assert [e.payload["component"] for e in store.read(types_=("ComponentParked",))] == ["store"]
    kent = [e for a, e in agents.calls if a == "kent"]
    assert kent[0]["context"]["kind"] == "blocked_cycle"
    assert component_events(store, "cart")[-1] == "ComponentImplemented"   # the rest goes on


def test_a_malformed_answer_outside_the_cycles_is_asked_again(repo):
    # the session of 2026-11-16: kent answered a ProposeRestart with its result under `change`; beck rejected
    # it and nothing asked again: the session stopped with nothing to do and nothing to answer
    def malformed(agents, envelope, cwd):
        return {"protocol": 1, "command": "ProposeRestart", "outcome": "done",
                "change": {"subject": "s", "components": []}}
    agents = Agents(repo, {("WriteRedTest", "store-adds"): [change("store"), None],
                           ("ProposeRestart", "interface"): [malformed, None]})
    engine, store = setup(repo, agents)
    run(engine)
    assert len(list(store.read(types_=("SignalRejected",)))) == 1
    proposals = [e for a, e in agents.calls if e["command"] == "ProposeRestart"]
    assert len(proposals) == 2 and "change: 'kind' is a required property" in proposals[1]["retry"]["reason"]
    assert [c.payload["command"] for c in pending_of_user(engine)] == ["ApproveRestart"]


def test_a_rejected_restart_brings_back_a_file_kent_removed(repo):
    # the session of 2026-11-16: no agent could delete a file, so the user did it by hand; kent and martin may
    # git rm now, and a rejected restart must bring the file back
    from beck.drivers.claude import allowed_tools
    from beck.machine.component import discard_restart
    assert "Bash(git rm:*)" in allowed_tools("kent") and "Bash(git rm:*)" in allowed_tools("martin")
    assert "Bash(git rm:*)" not in allowed_tools("unclebob")
    git(repo, "rm", "-q", "src/shop/store/store_adds.py")
    engine, store = setup(repo, Agents(repo))
    discard_restart(None, engine.ctx)
    assert (repo / "src/shop/store/store_adds.py").is_file() and git(repo, "status", "--short") == ""


def restart(repo, engine, component, scheduled):
    """A restart applied to the parked `component` that scheduled the components again."""
    cause = engine.record("ComponentParked", {"component": component, "reason": "unimplemented left",
                                              "because_of": "x"}, stream=f"component:{component}")
    engine.record("ExecutionDerived", scheduled, cause=cause)
    engine.record("RestartApplied", {"base": git(repo, "rev-parse", "HEAD"), "components": [component],
                                     "summary": "s"}, cause=cause)


def test_a_requirement_a_restart_adds_to_an_implemented_component_gets_its_cycle(repo):
    # the replay of 2026-11-16: kent gave program a new morphism; on resume beck took the implemented component for
    # finished, the unimplemented check reopened at green a requirement no cycle had opened, and its policy crashed
    agents = Agents(repo)
    engine, store = setup(repo, agents, initial=without("cart-empties"))
    run(engine)
    assert component_events(store, "cart")[-1] == "ComponentImplemented"
    restart(repo, engine, "cart", execution())
    run(engine)
    [opened] = [e for e in store.read(types_=("CycleOpened",)) if e.payload["requirement"] == "cart-empties"]
    assert opened.payload["unit"] == "morphism:empties"
    assert [e["context"]["requirement"] for a, e in agents.calls if e["command"] == "WriteRedTest"][-1] == "cart-empties"
    assert component_events(store, "cart")[-1] == "ComponentImplemented"
    assert not list(store.read(types_=("BeckCrashed", "CycleReopened")))


def test_a_requirement_a_restart_adds_to_a_closed_unit_gets_its_cycle(repo):
    scheduled = execution()
    scheduled["components"][1]["units"] = [{"id": "morphism:totals", "requirements": ["cart-totals"]}]
    scheduled["components"][1]["implements"] = ["cart-totals"]
    grown = execution()
    grown["components"][1]["units"] = [{"id": "morphism:totals", "requirements": ["cart-totals", "cart-empties"]}]
    agents = Agents(repo)
    engine, store = setup(repo, agents, initial=scheduled)
    run(engine)
    restart(repo, engine, "cart", grown)
    run(engine)
    assert [e.payload["requirement"] for e in store.read(types_=("CycleClosed",)) if e.payload["component"] == "cart"] == \
        ["cart-totals", "cart-empties"]
    assert component_events(store, "cart")[-1] == "ComponentImplemented"
