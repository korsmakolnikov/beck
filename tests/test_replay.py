"""The replay: turns read from a Claude Code transcript, matched to the new run's commands, re-applied to the files."""

import asyncio
import json

from beck.drivers.claude import session_id
from beck.dsl.validate import migrate
from beck.events.project import Project
from beck.replay import Recording, ReplayAgents, read_turn


def envelope(command_id, command="WriteGreenCode", retry=None):
    doc = {"protocol": 1, "agent": "unclebob", "command": command,
           "cause": {"event_id": command_id, "correlation_id": "c"}, "context": {}}
    if retry:
        doc["retry"] = {"attempt": retry, "reason": "r"}
    return doc


def user(text):
    return {"type": "user", "message": {"content": text}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def tool_use(id_, name, input_):
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def tool_result(id_, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": id_, "is_error": error}]}}


def answer(command):
    return {"type": "text", "text": json.dumps({"protocol": 1, "command": command, "outcome": "done", "result": {}})}


def test_a_turn_runs_from_its_envelope_to_the_next_and_keeps_its_last_text():
    transcript = [
        user(json.dumps(envelope("a"))),
        assistant({"type": "text", "text": "looking"}, tool_use("t1", "Write", {"file_path": "/p/x.py", "content": "x"})),
        tool_result("t1"),
        assistant(tool_use("t2", "Edit", {"file_path": "/p/x.py", "old_string": "y", "new_string": "z"})),
        tool_result("t2", error=True),
        assistant(answer("WriteGreenCode")),
        user(json.dumps(envelope("b"))),
        assistant(answer("WriteGreenCode")),
    ]
    output, tools = read_turn(transcript, "a")
    assert output["command"] == "WriteGreenCode"
    assert [(t["tool"], t["ok"]) for t in tools] == [("Write", True), ("Edit", False)]
    assert read_turn(transcript, "b")[1] == []
    assert read_turn(transcript, "missing") is None


def recorded(tmp_path, transcripts):
    """An original project with two WriteGreenCode turns of one session: a first and a retry."""
    project = Project(tmp_path / "original")
    it, store, started = project.start_iteration()
    sid = session_id(it.id, "unclebob", "u/g")
    lines = []
    for retry in (None, 1):
        args = {"session": "u/g", "context": {}, **({"retry": [retry, "r"]} if retry else {})}
        c = store.append("CommandIssued", {"command": "WriteGreenCode", "target": "unclebob", "args": args},
                         cause=started, policy=f"p{retry}")
        store.append("AgentTurnEnded", {"agent": "unclebob", "model": "m", "tokens": {"input": 0, "output": 0}}, cause=c)
        store.append("CommandDone", {"command": "WriteGreenCode"}, cause=c)
        target = f"{project.root}/src/x.py"
        lines += [user(json.dumps(envelope(c.id, retry=retry))),
                  assistant(tool_use(f"t{retry}", "Write", {"file_path": target, "content": f"v{retry}\n"})),
                  tool_result(f"t{retry}"), assistant(answer("WriteGreenCode"))]
    (transcripts / "dir").mkdir(parents=True)
    (transcripts / "dir" / f"{sid}.jsonl").write_text("\n".join(json.dumps(m) for m in lines))
    return Recording(store, it.id, project.root, transcripts)


def test_a_retry_the_new_code_does_not_ask_for_is_skipped(tmp_path):
    recording = recorded(tmp_path, tmp_path / "transcripts")
    first = recording.take("unclebob", "WriteGreenCode", "u/g", ("first", 0))
    assert first is not None and first.variant == ("first", 0)
    assert recording.take("unclebob", "WriteGreenCode", "u/g", ("first", 0)) is None
    assert recording.unused()["unclebob WriteGreenCode"] == 1


def test_a_replayed_turn_writes_its_files_in_the_new_folder(tmp_path):
    recording = recorded(tmp_path, tmp_path / "transcripts")
    new = tmp_path / "replay"
    (new / "src").mkdir(parents=True)
    agents = ReplayAgents(recording, new, "it")
    turn = asyncio.run(agents.turn("unclebob", "u/g", envelope("new"), {}, new))
    assert (new / "src/x.py").read_text() == "vNone\n"
    assert turn.tools[0]["input"]["file_path"] == f"{new}/src/x.py"
    asyncio.run(agents.turn("unclebob", "u/g", envelope("new2", retry=1), {}, new))
    assert (new / "src/x.py").read_text() == "v1\n"


def test_an_edit_whose_text_is_gone_is_counted_as_drift(tmp_path):
    agents = ReplayAgents(recorded(tmp_path, tmp_path / "t"), tmp_path, "it")
    (tmp_path / "f.py").write_text("a\n")
    agents._apply("Edit", {"file_path": str(tmp_path / "f.py"), "old_string": "b", "new_string": "c"}, tmp_path, {})
    assert (tmp_path / "f.py").read_text() == "a\n" and len(agents.drifted) == 1


def test_a_dsl_written_before_a_rename_is_migrated(tmp_path):
    agents = ReplayAgents(recorded(tmp_path, tmp_path / "t"), tmp_path, "it")
    (tmp_path / ".beck").mkdir(exist_ok=True)
    dsl = tmp_path / ".beck/architecture.yaml"
    agents._apply("Write", {"file_path": str(dsl), "content": "c:\n  realizes: [a]\n"}, tmp_path, {})
    agents._apply("Edit", {"file_path": str(dsl), "old_string": "realizes: [a]", "new_string": "realizes: [a, b]"},
                  tmp_path, {})
    assert dsl.read_text() == "c:\n  implements: [a, b]\n"


def test_migrate_renames_only_keys():
    assert migrate("- realizes: [x]\n  rule: it realizes: nothing\n") == "- implements: [x]\n  rule: it realizes: nothing\n"


def test_a_resumed_replay_does_not_take_again_what_it_took(tmp_path):
    recording = recorded(tmp_path, tmp_path / "transcripts")
    project = Project(tmp_path / "replay")
    _, store, started = project.start_iteration()
    c = store.append("CommandIssued", {"command": "WriteGreenCode", "target": "unclebob",
                                       "args": {"session": "u/g", "context": {}}}, cause=started, policy="p")
    store.append("AgentTurnEnded", {"agent": "unclebob", "model": "m", "tokens": {"input": 0, "output": 0}}, cause=c)
    recording.consume(store)
    assert recording.take("unclebob", "WriteGreenCode", "u/g", ("first", 0)) is None
    assert recording.take("unclebob", "WriteGreenCode", "u/g", ("retry", 1)) is not None


def test_a_turn_s_changes_are_a_patch_that_redoes_them(tmp_path):
    import subprocess

    from beck.machine.core import changes, snapshot

    def git(root, *args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    for d in ("a", "b"):
        root = tmp_path / d
        root.mkdir()
        git(root, "init", "-q")
        (root / "kept.py").write_text("x = 1\n")
        (root / "gone.py").write_text("y = 2\n")
    a, b = tmp_path / "a", tmp_path / "b"
    before = snapshot(a)
    (a / "kept.py").write_text("x = 2\n")
    (a / "gone.py").unlink()
    (a / "new.py").write_text("z = 3\n")
    patch = changes(a, before)
    subprocess.run(["git", "apply", "-"], cwd=b, input=patch, text=True, check=True)
    assert (b / "kept.py").read_text() == "x = 2\n" and not (b / "gone.py").exists()
    assert (b / "new.py").read_text() == "z = 3\n"
    assert changes(a, snapshot(a)) == ""
    (tmp_path / "nowhere").mkdir()
    assert snapshot(tmp_path / "nowhere") is None   # not a repository: no patch


def test_a_turn_recorded_in_the_events_replays_with_no_transcript(tmp_path):
    import subprocess

    from beck.machine.core import changes, snapshot
    original = tmp_path / "original"
    original.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=original, check=True)
    before = snapshot(original)
    (original / "x.py").write_text("x = 1\n")
    patch = changes(original, before)
    project = Project(original)
    it, store, started = project.start_iteration()
    c = store.append("CommandIssued", {"command": "WriteGreenCode", "target": "unclebob",
                                       "args": {"session": "u/g", "context": {}}}, cause=started, policy="p")
    answer_ = {"protocol": 1, "command": "WriteGreenCode", "outcome": "done"}
    store.append("AgentTurnEnded", {"agent": "unclebob", "model": "m", "tokens": {"input": 1, "output": 2},
                                    "output": answer_}, cause=c)
    store.append("TurnChanged", {"agent": "unclebob", "patch": patch}, cause=c)
    recording = Recording(store, it.id, project.root, tmp_path / "no-transcripts")
    assert recording.legacy == 0 and not recording.missing
    new = tmp_path / "replay"
    new.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=new, check=True)
    agents = ReplayAgents(recording, new, "it")
    turn = asyncio.run(agents.turn("unclebob", "u/g", envelope("n"), {}, new))
    assert turn.output == answer_ and (new / "x.py").read_text() == "x = 1\n" and not agents.drifted
