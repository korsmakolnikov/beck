"""The event store is what the policies, the resume, the audits and the metrics read: it is tested."""

import logging
import sqlite3

import pytest

from beck.cli import main
from beck.events import log
from beck.events.project import Project
from beck.events.store import EventStore, Message, fold
from beck.events.types import InvalidEvent

HASH = "0" * 64


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


def append_tests_run(store, cause=None, **extra):
    payload = {"command": "toolchain test", "tests": ["t1"], "report": "r.xml", "report_hash": HASH, "exit_code": 1}
    return store.append("TestsRun", payload, stream="cycle:a/c", actor="unclebob", cause=cause, **extra)


def test_an_event_without_a_cause_is_the_root_of_its_chain(store):
    e = store.append("SessionStarted", {"project": "/p"})
    assert e.seq == 1 and e.causation_id is None and e.correlation_id == e.id


def test_a_caused_event_keeps_the_cause_and_the_root(store):
    root = store.append("SessionStarted", {"project": "/p"})
    mid = store.append("BranchCreated", {"branch": "feat"}, cause=root)
    leaf = append_tests_run(store, cause=mid, sha="abc", tokens={"input": 1, "output": 2})
    assert (mid.causation_id, mid.correlation_id) == (root.id, root.id)
    assert (leaf.causation_id, leaf.correlation_id) == (mid.id, root.id)
    assert leaf.sha == "abc" and leaf.tokens == {"input": 1, "output": 2}


@pytest.mark.parametrize("type_, payload, message", [
    ("Nope", {}, "unknown event type 'Nope'"),
    ("SessionStarted", {}, "'project' is a required property"),
    ("SessionStarted", {"project": "/p", "extra": 1}, "Additional properties"),
    ("TestsRun", {"command": "c", "tests": [], "report": "r", "report_hash": "x", "exit_code": 0}, "report_hash"),
])
def test_a_malformed_event_never_enters_the_store(store, type_, payload, message):
    with pytest.raises(InvalidEvent, match=message):
        store.append(type_, payload)
    assert store.last() is None


def test_the_store_is_append_only(store):
    store.append("SessionStarted", {"project": "/p"})
    for sql in ("UPDATE event SET type = 'x'", "DELETE FROM event"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(sql)


def test_reading_by_stream_type_seq_and_chain(store):
    a = store.append("SessionStarted", {"project": "/p"})
    b = append_tests_run(store, cause=a)
    c = store.append("SessionStarted", {"project": "/q"})
    assert [e.id for e in store.read()] == [a.id, b.id, c.id]
    assert [e.id for e in store.read(stream="cycle:a/c")] == [b.id]
    assert [e.id for e in store.read(types_=("SessionStarted",))] == [a.id, c.id]
    assert [e.id for e in store.read(after=b.seq)] == [c.id]
    assert [e.id for e in store.read(correlation_id=a.id)] == [a.id, b.id]
    assert store.last() == c


def test_causes_and_effects(store):
    root = store.append("SessionStarted", {"project": "/p"})
    left = store.append("BranchCreated", {"branch": "x"}, cause=root)
    right = append_tests_run(store, cause=root)
    leaf = append_tests_run(store, cause=left)
    store.append("SessionStarted", {"project": "/other"})
    assert [e.id for e in store.causes(leaf.id)] == [root.id, left.id, leaf.id]
    assert [e.id for e in store.effects(root.id)] == [left.id, right.id, leaf.id]


def test_messages_and_projections_carry_the_event(store):
    root = store.append("SessionStarted", {"project": "/p"})
    run = append_tests_run(store, cause=root)
    message = Message.caused_by(run, "VerifyRed", "beck", {"requirement": "c"})
    assert (message.causation_id, message.correlation_id) == (run.id, root.id)
    projection = fold(store.read(), 0, lambda n, e: n + 1)
    assert (projection.state, projection.last_event_id, projection.last_seq) == (2, run.id, 2)
    assert fold([], "empty", lambda s, e: s).last_event_id is None


def test_log_lines_carry_the_event(tmp_path, store):
    handler = log.setup(tmp_path / "beck.log")
    try:
        root = store.append("SessionStarted", {"project": "/p"})
        run = append_tests_run(store, cause=root)
        logger = logging.getLogger("beck.test")
        logger.info("outside")
        with log.about(run):
            logger.info("verifying red")
    finally:
        logging.getLogger("beck").removeHandler(handler)
        handler.close()
    lines = (tmp_path / "beck.log").read_text().splitlines()
    assert "[event=- correlation=-] beck.test: outside" in lines[0]
    assert f"[event={run.id} correlation={root.id}] beck.test: verifying red" in lines[1]


def test_the_catalog_and_the_event_stores(tmp_path):
    project = Project(tmp_path)
    it, store, started = project.start_iteration()
    assert started.type == "SessionStarted" and started.payload == {"project": str(tmp_path.resolve())}
    assert project.store_path(it.id).is_file()
    assert "events/" in (tmp_path / ".beck" / ".gitignore").read_text()
    project.name_iteration(it.id, "feat/x")
    project.end_iteration(it.id)
    assert project.iteration(it.id).name == "feat/x" and project.iteration(it.id).ended_at
    assert project.find(it.id[:6]) == project.iteration(it.id)
    with pytest.raises(KeyError):
        project.find("zz")
    store.close()
    assert project.open(it.id).last() == started


def test_cli(tmp_path, capsys):
    project = Project(tmp_path)
    it, store, started = project.start_iteration()
    run = append_tests_run(store, cause=started)
    store.close()
    project.name_iteration(it.id, "feat/x")
    assert main(["sessions", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert it.id[:8] in out and "feat/x" in out and "TestsRun" in out
    assert main(["events", it.id[:8], "--correlation", started.id[:8], "--project", str(tmp_path)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    assert main(["trace", it.id[:8], run.id[:8], "--project", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("  ") and "SessionStarted" in lines[0]
    assert lines[1].startswith("> ") and "TestsRun" in lines[1]
