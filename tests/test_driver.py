"""The Claude Code driver, against a fake `claude` that speaks the same stream-json."""

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from beck.drivers.claude import ClaudeCode, allowed_tools, definition, parse_answer, session_id
from beck.events.project import Project
from beck.events.store import EventStore
from beck.machine.core import TurnFailed

FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps({{"argv": args, "stdin": sys.stdin.read(),
                                                    "cwd": os.getcwd(), "cause": os.environ.get("BECK_CAUSE")}}) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "crash":
    sys.stderr.write("boom\n"); sys.exit(1)
if mode == "in-use" and "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
    sys.stderr.write(f"Error: Session ID {{sid}} is already in use.\n"); sys.exit(1)
print(json.dumps({{"type": "system", "subtype": "init"}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": "working"}},
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "beck test x"}}}}]}}}}))
answer = "```json\n" + json.dumps({{"protocol": 1, "command": "SummariseOutput", "outcome": "done",
                                   "result": {{"ok": True, "summary": "fine"}}}}) + "\n```"
print(json.dumps({{"type": "result", "subtype": "success", "is_error": mode == "error", "result": answer,
                  "usage": {{"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 100}}}}))
'''

ENVELOPE = {"protocol": 1, "agent": "reader", "command": "SummariseOutput",
            "cause": {"event_id": "0b5b0d3e-0000-4000-8000-000000000001",
                      "correlation_id": "0b5b0d3e-0000-4000-8000-000000000000"},
            "context": {"log": ".beck/reports/1.log", "tool": "pytest"}}


@pytest.fixture
def driver(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    (tmp_path / ".beck").mkdir()
    store = EventStore(tmp_path / "e.db")
    yield ClaudeCode(store, tmp_path, "iter-1", {"reader": "haiku"}, executable=str(fake), docs=tmp_path / "docs")
    store.close()


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_a_turn_sends_the_envelope_and_reads_the_answer_tokens_and_tools(driver, tmp_path):
    turn = asyncio.run(driver.turn("reader", "summary/1", ENVELOPE, {"BECK_CAUSE": "c-1"}, tmp_path))
    assert turn.output == {"protocol": 1, "command": "SummariseOutput", "outcome": "done",
                           "result": {"ok": True, "summary": "fine"}}
    assert turn.tokens == {"input": 12, "output": 3, "cache_read": 100, "cache_creation": 0}
    assert turn.tools == [{"tool": "Bash", "input": {"command": "beck test x"}}]
    assert turn.session == session_id("iter-1", "reader", "summary/1") and turn.model == "haiku"
    [call] = calls(tmp_path)
    assert json.loads(call["stdin"]) == ENVELOPE and call["cause"] == "c-1"
    argv = call["argv"]
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--agent") + 1] == "reader" and argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--session-id") + 1] == turn.session and "--resume" not in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"


def test_the_next_turn_of_a_session_resumes_it(driver, tmp_path):
    sid = session_id("iter-1", "reader", "summary/1")
    driver.store.append("AgentTurnEnded", {"agent": "reader", "model": "haiku", "session": sid,
                                           "tokens": {"input": 1, "output": 1}})
    asyncio.run(driver.turn("reader", "summary/1", ENVELOPE, {}, tmp_path))
    argv = calls(tmp_path)[0]["argv"]
    assert argv[argv.index("--resume") + 1] == sid and "--session-id" not in argv


@pytest.mark.parametrize("mode, message", [("crash", "boom"), ("error", "SummariseOutput")])
def test_a_failed_turn_raises_turn_failed(driver, tmp_path, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(TurnFailed, match=message):
        asyncio.run(driver.turn("reader", "s", ENVELOPE, {}, tmp_path))


def test_the_agents_file_holds_the_prompts_with_the_commands_examples(driver, tmp_path):
    asyncio.run(driver.turn("reader", "s", ENVELOPE, {}, tmp_path))
    agents = json.loads((tmp_path / ".beck/agents.json").read_text())
    assert set(agents) == {"kent", "unclebob", "martin", "reader"}
    unclebob = agents["unclebob"]
    assert "### WriteRedTest" in unclebob["prompt"] and "### WriteGreenCode" in unclebob["prompt"]
    assert "Answer with **one JSON document and nothing else**" in unclebob["prompt"]
    assert unclebob["tools"] == ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]
    assert "Bash(beck send:*)" in allowed_tools("unclebob") and "Bash(beck test:*)" not in allowed_tools("unclebob")
    assert agents["reader"]["model"] == "haiku" and definition("reader")["tools"] == ["Read"]


@pytest.mark.parametrize("text, parsed", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here it is: {"a": {"b": 2}} done', {"a": {"b": 2}}),
    ("no json", "no json"),
])
def test_the_answer_is_the_json_document(text, parsed):
    assert parse_answer(text) == parsed


def test_a_relative_project_gives_an_agent_in_a_worktree_absolute_paths(driver, tmp_path, monkeypatch):
    # `beck tui` run in the project makes the root ".": unclebob's turn, in a component's worktree, did not find
    # ./.beck/agents.json (the session of 2026-11-15, WriteRedTest failed three times)
    monkeypatch.chdir(tmp_path)
    relative = ClaudeCode(driver.store, Path("."), "iter-1", executable=driver.executable)
    worktree = tmp_path / ".beck/worktrees/model"
    worktree.mkdir(parents=True)
    asyncio.run(relative.turn("reader", "s", ENVELOPE, {}, worktree))
    argv = calls(tmp_path)[-1]["argv"]
    agents = Path(argv[argv.index("--agents") + 1])
    assert agents.is_absolute() and agents == tmp_path / ".beck/agents.json"


def test_a_project_opened_from_a_relative_root_holds_it_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Project(".").root == tmp_path


def test_a_session_a_failed_turn_started_is_resumed(driver, tmp_path, monkeypatch):
    # the session of 2026-11-16: a turn failed on the usage limit after Claude Code had created its session, so
    # beck, which found no AgentTurnEnded of it, asked for it new again: "Session ID ... is already in use"
    monkeypatch.setenv("FAKE_MODE", "in-use")
    turn = asyncio.run(driver.turn("reader", "s", ENVELOPE, {}, tmp_path))
    assert turn.output["outcome"] == "done"
    first, second = (c["argv"] for c in calls(tmp_path))
    sid = session_id("iter-1", "reader", "s")
    assert first[first.index("--session-id") + 1] == sid and second[second.index("--resume") + 1] == sid
