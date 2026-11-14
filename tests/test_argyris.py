"""The outbox's reader: a structured message for argyris, and argyris's answer settling the records."""

import io
import json
import stat

from test_audit import run
from test_cycle import repo  # noqa: F401

from beck import argyris
from beck.agents import protocol
from beck.cli import main


def violated(repo):
    return run(repo, {"WriteRedTest": [{"tool": "Bash", "input": {"command": "pytest"}},
                                       {"tool": "WebFetch", "input": {"url": "https://example.com"}}]})


def answer(ids, how="proposed"):
    return {"protocol": 1, "command": "ReviewViolations", "outcome": "done", "result": {"settled": [
        {"id": i, "how": how, "pr": "https://github.com/owner/beck/pull/3",
         "files": ["src/beck/agents/prompts/unclebob.md"], "reason": "the rule is explicit"} for i in ids]}}


def test_the_message_is_a_valid_input_naming_the_files_of_each_rule(repo):
    project, _ = violated(repo)
    doc = argyris.message(project)
    protocol.check_input(doc)
    v = doc["context"]["violations"]
    assert sorted(x["kind"] for x in v) == ["forbidden-command", "forbidden-tool"]
    assert v[0]["agent"] == "unclebob" and v[0]["rules"][0] == f"{argyris.package_prefix()}/agents/prompts/unclebob.md"
    assert "agent" not in v[0]["detail"]
    assert all((argyris.contrib.source({}) / r).is_file() for r in v[0]["rules"])


def test_nothing_pending_sends_nothing(tmp_path):
    from beck.events.project import Project
    assert argyris.message(Project(tmp_path)) is None
    assert argyris.wake(Project(tmp_path), executable="/bin/false") == 1


def test_the_answer_settles_the_records_and_records_it_in_their_iteration(repo):
    project, store = violated(repo)
    first, second = project.pending()
    assert argyris.settle(project, answer([first.id])) == []
    assert [r.id for r in project.pending()] == [second.id]
    assert project.outbox(first.id).handled_how["pr"] == "https://github.com/owner/beck/pull/3"
    settled = next(iter(store.read(types_=("ViolationSettled",))))
    assert settled.actor == "argyris" and settled.causation_id == first.event_id
    assert settled.correlation_id == first.correlation_id


def test_an_invalid_answer_settles_nothing(repo):
    project, _ = violated(repo)
    ids = [r.id for r in project.pending()]
    assert argyris.settle(project, answer(ids, how="maybe"))
    assert argyris.settle(project, answer(["not-a-record"])) == ["settled.id: not pending in the outbox: not-a-record"]
    assert len(project.pending()) == 2


def test_the_cli_prints_the_message_and_reads_the_answer(repo, capsys, monkeypatch):
    project, _ = violated(repo)
    assert main(["audit", "--message", "--project", str(repo)]) == 0
    doc = json.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(answer([v["id"] for v in doc["context"]["violations"]]))))
    assert main(["audit", "--settle", "--project", str(repo)]) == 0
    assert project.pending() == []
    assert main(["audit", "--message", "--project", str(repo)]) == 1


def test_a_proposal_needs_its_pull_request(repo):
    project, _ = violated(repo)
    doc = answer([project.pending()[0].id])
    del doc["result"]["settled"][0]["pr"]
    assert argyris.settle(project, doc)


def test_wake_opens_beck_s_argyris_in_beck_s_source_with_the_message(repo, tmp_path):
    project, _ = violated(repo)
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\npwd > {tmp_path}/cwd\nprintf '%s\\n' \"$@\" > {tmp_path}/argv\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert argyris.wake(project, executable=str(fake)) == 0
    assert (tmp_path / "cwd").read_text().strip() == str(argyris.contrib.source({}))
    argv = (tmp_path / "argv").read_text()
    assert "beck audit --settle --project" in argv and '"command": "ReviewViolations"' in argv
    assert "--agent\nargyris\n--allowedTools\n" in argv and "Bash(beck contrib:*)" in argv
    defined = json.loads((repo / ".beck/argyris.json").read_text())["argyris"]
    assert "beck contrib submit" in defined["prompt"] and "How you work with beck" not in defined["prompt"]
