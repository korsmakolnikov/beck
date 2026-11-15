"""The cycle's machine, end to end: a real repository, real pytest runs through beck test, a scripted agent."""

import asyncio
import os
import subprocess
import sys

import pytest

from beck.events.project import Project
from beck.machine.core import Ctx, Reactor, Turn
from beck.machine.cycle import Workspace, cycle
from beck.machine.signatures import blocked, normalise, repeated, signature
from beck.testrun import run as run_tests
from beck.toolchain import load

PYTEST = f'[{sys.executable}, -m, pytest, -q, -p, no:cacheprovider, "--junitxml={{report}}", "{{tests}}"]'
TEST = "tests/test_mod.py::test_double"
RED_TEST = "from pkg.mod import double\n\n\ndef test_double():\n    # spec: doubles\n    assert double(2) == 4\n"
PASSING_TEST = "def test_double():\n    # spec: doubles\n    assert True\n"
CODE = "def double(x):\n    return 2 * x\n"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n")
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/__init__.py").touch()
    (tmp_path / "src/pkg/mod.py").write_text("def double(x):\n    raise NotImplementedError\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck/toolchain.yml").write_text(f"toolchain: 1\nlanguage: python\ntest: {PYTEST}\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "beck@example.com")
    git(tmp_path, "config", "user.name", "beck")
    Project(tmp_path)  # writes .beck/.gitignore
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "start")
    for var in ("BECK_CAUSE", "BECK_ITERATION", "BECK_ACTOR", "BECK_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class Agent:
    """Plays unclebob from a script: per command, one step per attempt (the last one repeats)."""

    def __init__(self, root, script, unit="u"):
        self.root, self.script, self.calls, self.unit = root, script, [], unit

    async def turn(self, agent, session, envelope, env, cwd):
        self.calls.append(envelope)
        steps = self.script[envelope["command"]]
        n = sum(1 for c in self.calls if c["command"] == envelope["command"])
        step = steps[min(n, len(steps)) - 1]
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            output = step(self)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        return Turn(output, "fake", {"input": 10, "output": 5})

    def write(self, path, text):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def test(self, selector=TEST):
        run_tests(self.root, load(self.root), [selector], actor="unclebob", unit=self.unit, clause="doubles")


def red(test_text=RED_TEST, run=True, answer=TEST):
    def step(a):
        a.write("tests/test_mod.py", test_text)
        if run:
            a.test()
        return {"protocol": 1, "command": "WriteRedTest", "outcome": "done", "result": {"test": answer}}
    return step


def green(code=CODE, also=None):
    def step(a):
        a.write("src/pkg/mod.py", code)
        if also:
            a.write(*also)
        a.test()
        return {"protocol": 1, "command": "WriteGreenCode", "outcome": "done"}
    return step


def machine(repo, agent, unit="u"):
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace({unit: repo}, {unit: ["src/pkg"]}, {(unit, "operation:double"): {
        "unit": {"id": unit}, "group": "operation:double", "clauses": [], "entities": [], "dependencies": [],
        "files": {"source": [], "tests": []}}})
    ctx = Ctx(store, repo, it.id, workspace)
    opened = store.append("CycleOpened", {"unit": unit, "clause": "doubles", "group": "operation:double",
                                          "first": True}, stream=f"cycle:{unit}/doubles")
    return Reactor(ctx, cycle, agent), store, opened


def types(store):
    return [e.type for e in store.read() if e.type not in ("CommandIssued", "CommandDone", "AgentTurnEnded",
                                                            "TestsRun")]


def test_a_cycle_goes_red_then_green_and_commits_both(repo):
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [green()]})
    reactor, store, opened = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert types(store) == ["SessionStarted", "CycleOpened", "RedDeclared", "RedVerified", "RedCommitted",
                            "GreenDeclared", "GreenVerified", "GreenCommitted", "CycleClosed"]
    assert git(repo, "log", "--format=%s").splitlines() == ["feat(u): doubles", "test(u): doubles", "start"]
    assert all(e.correlation_id == opened.id for e in store.read() if e.seq >= opened.seq)
    first = agent.calls[0]
    assert first["context"]["group"]["group"] == "operation:double"
    assert "group" not in agent.calls[1]["context"] and agent.calls[1]["context"]["test"] == TEST
    runs = [e for e in store.read(types_=("TestsRun",))]
    assert [store.get(r.causation_id).payload["command"] for r in runs] == ["WriteRedTest", "WriteGreenCode"]


def test_a_red_that_passes_is_sent_back_with_the_reason(repo):
    agent = Agent(repo, {"WriteRedTest": [red(PASSING_TEST), red()], "WriteGreenCode": [green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert "RedRejected" in types(store) and types(store)[-1] == "CycleClosed"
    assert agent.calls[1]["retry"] == {"attempt": 2, "reason": "the test passes already: a red test must fail"}


def test_a_run_older_than_the_files_is_stale(repo):
    def edit_after(a):
        red()(a)
        a.write("tests/test_mod.py", RED_TEST + "\n# changed after the run\n")
        return {"protocol": 1, "command": "WriteRedTest", "outcome": "done", "result": {"test": TEST}}
    agent = Agent(repo, {"WriteRedTest": [edit_after, red()], "WriteGreenCode": [green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    rejected = [e for e in store.read(types_=("RedRejected",))]
    assert rejected[0].payload["reason"] == "files changed after the last beck test run"


def test_a_green_that_changes_a_test_or_leaves_its_scope_is_rejected(repo):
    def then(path, text, remove=None):
        def step(a):
            if remove:
                (repo / remove).unlink()
            return green(also=(path, text) if path else None)(a)
        return step
    agent = Agent(repo, {"WriteRedTest": [red()],
                         "WriteGreenCode": [then("tests/test_other.py", "x = 1\n"),
                                            then("docs/notes.md", "x\n", remove="tests/test_other.py"),
                                            then(None, None, remove="docs/notes.md")]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    reasons = [e.payload["reason"] for e in store.read(types_=("GreenRejected",))]
    assert reasons == ["a green changes no test; changed: tests/test_other.py",
                       "outside the unit's scope: docs/notes.md"]
    assert types(store)[-1] == "CycleClosed"


def test_a_failing_green_carries_the_summary_to_the_next_turn(repo):
    agent = Agent(repo, {"WriteRedTest": [red()],
                         "WriteGreenCode": [green("def double(x):\n    return x\n"), green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    retry = agent.calls[2]
    assert retry["context"]["failure"].startswith("NOT_OK (1 of 1 not passed)")
    assert retry["retry"]["reason"] == "the test does not pass"


def test_a_malformed_answer_is_a_signal_rejected_and_retried(repo):
    bad = lambda a: {"protocol": 1, "command": "WriteRedTest", "outcome": "done"}  # noqa: E731
    agent = Agent(repo, {"WriteRedTest": [bad, red()], "WriteGreenCode": [green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    rejected = list(store.read(types_=("SignalRejected",)))
    assert rejected and "'result' is a required property" in rejected[0].payload["errors"][0]
    assert types(store)[-1] == "CycleClosed"


def test_a_green_retried_for_its_format_alone_keeps_the_run_of_the_rejected_turn(repo):
    # the session of 2026-11-15: unclebob ran the test, answered with a `result` the schema forbids, then, asked
    # again, fixed the answer alone; VerifyGreen looked for a run of the second turn and rejected the green
    def run_then_malformed(a):
        green()(a)
        return {"protocol": 1, "command": "WriteGreenCode", "outcome": "done", "result": {}}
    fixed = lambda a: {"protocol": 1, "command": "WriteGreenCode", "outcome": "done"}  # noqa: E731
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [run_then_malformed, fixed]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert "GreenRejected" not in types(store) and types(store)[-1] == "CycleClosed"


def test_a_run_of_a_rejected_turn_is_stale_once_the_files_change(repo):
    def run_then_malformed(a):
        green()(a)
        return {"protocol": 1, "command": "WriteGreenCode", "outcome": "done", "result": {}}
    def edit_without_running(a):
        a.write("src/pkg/mod.py", CODE + "\n")
        return {"protocol": 1, "command": "WriteGreenCode", "outcome": "done"}
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [run_then_malformed, edit_without_running,
                                                                    green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    [rejected] = store.read(types_=("GreenRejected",))
    assert rejected.payload["reason"] == "files changed after the last beck test run"
    assert types(store)[-1] == "CycleClosed"


def wrong_test(a):
    return {"protocol": 1, "command": "WriteGreenCode", "outcome": "change_requested",
            "change": {"kind": "test", "target": "doubles", "problem": "the red test builds an invalid input",
                       "proposal": "rewrite the red test with a valid input"}}


def test_a_green_that_finds_the_red_test_wrong_takes_the_cycle_back_to_red(repo):
    # the session of 2026-11-15: a committed red test became wrong after a resolution; a green may not change a
    # test, kent found nothing to change in the model, and the unit went round asking for a change
    agent = Agent(repo, {"WriteRedTest": [red(), red()], "WriteGreenCode": [wrong_test, green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert types(store) == ["SessionStarted", "CycleOpened", "RedDeclared", "RedVerified", "RedCommitted",
                            "ChangeRequested", "RedRewound", "RedDeclared", "RedVerified", "RedCommitted",
                            "GreenDeclared", "GreenVerified", "GreenCommitted", "CycleClosed"]
    assert git(repo, "log", "--format=%s").splitlines() == ["feat(u): doubles", "test(u): doubles", "start"]
    second_red = [c for c in agent.calls if c["command"] == "WriteRedTest"][1]
    assert second_red["retry"]["reason"] == "the red test is wrong: the red test builds an invalid input"


def test_a_red_taken_back_too_often_blocks_the_cycle(repo):
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [wrong_test]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert types(store).count("RedRewound") == 3 and types(store)[-1] == "CycleBlocked"


def test_the_same_rejection_three_times_blocks_the_cycle(repo):
    agent = Agent(repo, {"WriteRedTest": [red(PASSING_TEST)], "WriteGreenCode": [green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert types(store)[-1] == "CycleBlocked"
    assert types(store).count("RedRejected") == 3
    assert list(store.read(types_=("CycleBlocked",)))[0].payload["reason"] == "the same result 3 times in a row"


def test_a_change_request_ends_the_cycle_s_turns(repo):
    change = lambda a: {"protocol": 1, "command": "WriteRedTest", "outcome": "change_requested",  # noqa: E731
                        "change": {"kind": "interface", "target": "double", "problem": "p", "proposal": "q"}}
    agent = Agent(repo, {"WriteRedTest": [change]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert types(store)[-1] == "ChangeRequested" and len(agent.calls) == 1


def test_a_replay_issues_nothing_again(repo):
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [green()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    count = len(list(store.read()))
    again = Reactor(reactor.ctx, cycle, agent)
    asyncio.run(again.run_until_idle())
    assert len(list(store.read())) == count and len(agent.calls) == 2


def test_an_interrupted_command_runs_once_on_attach(repo):
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [green()]})
    reactor, store, _ = machine(repo, agent)
    reactor.react()                                  # WriteRedTest issued, then "Ctrl-C"
    assert [e.payload["command"] for e in reactor.pending()] == ["WriteRedTest"]
    attached = Reactor(reactor.ctx, cycle, agent)
    asyncio.run(attached.run_until_idle())
    assert types(store)[-1] == "CycleClosed" and len(agent.calls) == 2


def test_signatures_ignore_lines_addresses_and_times():
    assert normalise("a.py:12:3 failed at 0x7f00 in 12 ms") == "a.py:_ failed at 0x_ in <time>"
    assert signature(["x.py:1 boom", "y"]) == signature(["y", "x.py:99 boom"])


@pytest.mark.parametrize("sequence, p", [
    (["a", "a", "a"], 1), (["b", "a", "a", "a"], 1), (["a", "b", "a", "b", "a", "b"], 2),
    (["a", "a"], None), (["a", "b", "a", "b"], None), (["a", "b", "c"], None),
])
def test_the_repetition_rule(sequence, p):
    assert repeated(sequence) == p


def test_thirty_attempts_block_too():
    assert blocked([str(i) for i in range(30)]) == "30 attempts"
    assert blocked([str(i) for i in range(29)]) is None


def test_undoing_a_red_leaves_a_greened_clause_and_a_conflict_alone(repo):
    # the session of 2026-11-15: the dropped clause had its green too, later cycles built on its test, and the
    # revert of its red stopped in a conflict inside the handler
    from beck.machine.cycle import undo_red
    def commit(message, text):
        (repo / "tests/test_mod.py").write_text(text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", message)
    commit("test(u): doubles", RED_TEST)
    commit("feat(u): doubles", RED_TEST)
    head = git(repo, "rev-parse", "HEAD")
    undo_red(repo, "u", "doubles")                      # greened: its test stays
    assert git(repo, "rev-parse", "HEAD") == head
    commit("test(u): halves", RED_TEST + "\n\ndef test_half():\n    pass\n")
    commit("test(u): later", RED_TEST + "\n\ndef test_half():\n    assert True\n")
    head = git(repo, "rev-parse", "HEAD")
    undo_red(repo, "u", "halves")                       # the revert conflicts with the later commit
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--short") == ""


def test_an_integration_test_that_passes_at_once_proves_the_clause(repo):
    # the session of 2026-11-15: every component landed, so the integration test of every-match-ends-with-a-winner
    # passed on its first run, faithful to the property; beck refused it as a red that passes
    (repo / "src/pkg/mod.py").write_text(CODE)
    git(repo, "commit", "-qam", "the components landed")
    agent = Agent(repo, {"WriteRedTest": [red()], "WriteGreenCode": [green()]}, unit="@integration")
    reactor, store, _ = machine(repo, agent, unit="@integration")
    asyncio.run(reactor.run_until_idle())
    assert types(store) == ["SessionStarted", "CycleOpened", "RedDeclared", "RedVerified", "RedCommitted",
                            "CycleClosed"]
    assert [c["command"] for c in agent.calls] == ["WriteRedTest"]
    assert git(repo, "log", "-1", "--format=%s") == "test(@integration): doubles"


def test_a_component_test_that_passes_at_once_is_still_refused(repo):
    (repo / "src/pkg/mod.py").write_text(CODE)
    git(repo, "commit", "-qam", "already there")
    agent = Agent(repo, {"WriteRedTest": [red()]})
    reactor, store, _ = machine(repo, agent)
    asyncio.run(reactor.run_until_idle())
    assert "RedRejected" in types(store) and "RedCommitted" not in types(store)
