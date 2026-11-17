"""The replay: the recorded turns and decisions, read from the events alone, answered to the new run."""

import asyncio
import subprocess

from beck.events.project import Project
from beck.machine.core import Ctx, Engine, Machine, changes, snapshot
from beck.replay import Recording, ReplayAgents


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "pyproject.toml").write_text("")
    (path / ".beck").mkdir()
    (path / ".beck/toolchain.yml").write_text("toolchain: 1\nlanguage: python\ntest: [\"true\"]\n")
    return path


def recorded(tmp_path):
    """An original iteration: a WriteGreenCode turn (its output, its patch, its beck send test) and its retry."""
    original = repo(tmp_path / "original")
    project = Project(original)
    it, store, started = project.start_iteration()
    for retry in (None, 1):
        args = {"session": "u/g", "context": {}, **({"retry": [retry, "r"]} if retry else {})}
        request = store.append("AgentTurnRequested", {"command": "WriteGreenCode", "target": "unclebob", "args": args},
                               cause=started, policy="p")
        before = snapshot(original)
        (original / "x.py").write_text(f"x = {retry}\n")
        store.append("TestsRun", {"command": "true", "tests": ["tests/test_x.py"], "exit_code": 0, "component": "u"},
                     actor="unclebob", cause=request)
        store.append("AgentTurnEnded", {"agent": "unclebob", "model": "m", "tokens": {"input": 1, "output": 2},
                                        "output": {"protocol": 1, "command": "WriteGreenCode", "outcome": "done",
                                                   "retry": retry}}, cause=request)
        store.append("TurnChanged", {"agent": "unclebob", "patch": changes(original, before)}, cause=request)
    return Recording(store, project.root)


def envelope(retry=None):
    return {"protocol": 1, "agent": "unclebob", "command": "WriteGreenCode", "context": {},
            **({"retry": {"attempt": retry, "reason": "r"}} if retry else {})}


def test_a_retry_the_new_code_does_not_ask_for_is_skipped(tmp_path):
    recording = recorded(tmp_path)
    first = recording.take("unclebob", "WriteGreenCode", "u/g", ("first", 0))
    assert first is not None and first.variant == ("first", 0) and first.runs == [
        ("test", {"tests": ["tests/test_x.py"], "component": "u", "requirement": None})]
    assert recording.take("unclebob", "WriteGreenCode", "u/g", ("first", 0)) is None
    assert recording.unused()["unclebob WriteGreenCode"] == 1


def test_a_replayed_turn_applies_its_patch_and_sends_its_runs_again(tmp_path):
    recording = recorded(tmp_path)
    new = repo(tmp_path / "replay")
    project = Project(new)
    it, store, started = project.start_iteration()
    Engine(Ctx(store, project.root, it.id, None), Machine(), None)   # the replay's session: its socket
    agents = ReplayAgents(recording, project.root, it.id)
    env = {"BECK_CAUSE": started.id}
    turn = asyncio.run(agents.turn("unclebob", "u/g", envelope(), env, project.root))
    assert (new / "x.py").read_text() == "x = None\n" and turn.output["retry"] is None and not agents.drifted
    [run] = store.read(types_=("TestsRun",))
    assert run.causation_id == started.id and run.actor == "unclebob" and run.payload["component"] == "u"
    asyncio.run(agents.turn("unclebob", "u/g", envelope(retry=1), env, project.root))
    assert (new / "x.py").read_text() == "x = 1\n"


def test_a_resumed_replay_does_not_take_again_what_it_took(tmp_path):
    recording = recorded(tmp_path)
    project = Project(tmp_path / "replay")
    _, store, started = project.start_iteration()
    request = store.append("AgentTurnRequested", {"command": "WriteGreenCode", "target": "unclebob",
                                                  "args": {"session": "u/g", "context": {}}}, cause=started, policy="p")
    store.append("AgentTurnEnded", {"agent": "unclebob", "model": "m", "tokens": {"input": 0, "output": 0}},
                 cause=request)
    recording.consume(store)
    assert recording.take("unclebob", "WriteGreenCode", "u/g", ("first", 0)) is None
    assert recording.take("unclebob", "WriteGreenCode", "u/g", ("retry", 1)) is not None


def test_a_decision_is_answered_as_recorded_by_its_key(tmp_path):
    project = Project(repo(tmp_path / "original"))
    _, store, started = project.start_iteration()
    for key, words in (("goal", "Pong."), ("players", "Two.")):
        asked = store.append("DecisionRequested", {"command": "AskInterview", "target": "user",
                                                   "args": {"key": key, "question": "?"}}, cause=started)
        store.append("InterviewAnswered", {"key": key, "question": "?", "answer": words}, actor="user", cause=asked)
    recording = Recording(store, project.root, turns=False)
    later = store.append("DecisionRequested", {"command": "AskInterview", "target": "user",
                                               "args": {"key": "players", "question": "?"}}, cause=started)
    [answer] = recording.decide(later)
    assert answer.payload["answer"] == "Two." and answer.actor == "user"
