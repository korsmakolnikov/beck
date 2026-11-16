"""The protocol is the contract between beck and the agents: every command is checked both ways."""

from pathlib import Path

import pytest
import yaml

from beck.agents import protocol

EXAMPLES = yaml.safe_load((Path(__file__).parents[1] / "src" / "beck" / "agents" / "examples.yaml").read_text())
CAUSE = ("0b5b0d3e-0000-4000-8000-000000000001", "0b5b0d3e-0000-4000-8000-000000000000")
ALL = [(a, c) for a in protocol.AGENTS for c in protocol.commands(a)]


def out(command, body):
    return {"protocol": 1, "command": command, **body}


def test_every_command_has_an_example_and_every_example_a_command():
    assert sorted(ALL) == sorted((a, c) for a, cs in EXAMPLES.items() for c in cs)


@pytest.mark.parametrize("agent, command", ALL)
def test_the_examples_are_valid(agent, command):
    example = EXAMPLES[agent][command]
    doc = protocol.envelope(agent, command, *CAUSE, example["in"])
    assert doc["cause"] == {"event_id": CAUSE[0], "correlation_id": CAUSE[1]}
    checked = protocol.check_output(agent, command, out(command, example["out"]))
    assert checked.errors == []


def test_a_retry_carries_the_reason():
    doc = protocol.envelope("martin", "ResolveConflict", *CAUSE, {"files": ["mix.exs"], "landed": [], "run": "r"},
                            retry=(2, "a test changed"))
    assert doc["retry"] == {"attempt": 2, "reason": "a test changed"}


@pytest.mark.parametrize("agent, command, context, message", [
    ("unclebob", "Nope", {}, "unclebob has no command 'Nope'"),
    ("unclebob", "WriteRedTest", {"clause": "c"}, "context.<root>: 'run' is a required property"),
    ("unclebob", "WriteRedTest", {"clause": "c", "run": "r", "extra": 1}, "Additional properties"),
    ("ghost", "WriteRedTest", {}, "agent"),
])
def test_beck_never_sends_a_wrong_input(agent, command, context, message):
    with pytest.raises(protocol.ProtocolError, match=message):
        protocol.envelope(agent, command, *CAUSE, context)


@pytest.mark.parametrize("agent, command, body, message", [
    ("unclebob", "WriteRedTest", {"outcome": "done"}, "'result' is a required property"),
    ("unclebob", "WriteRedTest", {"outcome": "question", "question": {"text": "t", "why": "w"}}, "is not one of"),
    ("unclebob", "WriteGreenCode", {"outcome": "done", "result": {}}, "should not be valid"),
    ("kent", "AnchorIds", {"outcome": "change_requested",
                           "change": {"kind": "interface", "target": "x", "problem": "p", "proposal": "q"}},
     "'done' was expected"),
    ("martin", "RefactorUnit", {"outcome": "done", "result": {"phase": "blue", "addressed": ["F1"]}}, "'blue'"),
    ("kent", "WriteDomain", {"outcome": "question"}, "'question' is a required property"),
    ("kent", "WriteDomain", {"outcome": "done", "question": {"text": "t", "why": "w"}}, "should not be valid"),
    ("kent", "AskFollowUps", {"outcome": "done", "result": {"questions": []}, "chat": "hi"}, "Additional properties"),
])
def test_a_wrong_output_is_rejected_with_its_errors(agent, command, body, message):
    checked = protocol.check_output(agent, command, out(command, body))
    assert checked.output is None
    assert any(message in e for e in checked.errors), checked.errors


def test_an_answer_to_another_command_is_rejected():
    checked = protocol.check_output("martin", "ResolveConflict", out("ReviewUnit", {"outcome": "done"}))
    assert checked.errors == ["command: the answer is for 'ReviewUnit', not 'ResolveConflict'"]


def test_only_kent_asks_the_user(monkeypatch):
    checked = protocol.check_output("reader", "SummariseOutput",
                                    out("SummariseOutput", {"outcome": "question", "question": {"text": "t", "why": "w"}}))
    assert checked.output is None


QUESTION = {"text": "Are archived bookmarks taggable?", "why": "The invariant depends on it."}


def test_the_answer_of_the_user_is_wrapped_for_the_same_command():
    doc = protocol.answer("kent", "WriteDomain", *CAUSE, QUESTION, "No, never.")
    assert doc["context"] == {} and doc["answer"] == {"question": QUESTION, "text": "No, never."}


def test_an_answer_does_not_resend_the_context():
    doc = protocol.answer("kent", "WriteDomain", *CAUSE, QUESTION, "No.")
    doc["context"] = {"file": ".beck/domain.yaml"}
    with pytest.raises(protocol.ProtocolError, match="send {}"):
        protocol.check_input(doc)


def test_martin_strengthens_weak_tests_or_addresses_findings():
    weak = {"test": "test/a_test.exs:3", "clause": "c", "why": "It asserts nothing."}
    protocol.envelope("martin", "RefactorUnit", *CAUSE, {"weak_tests": [weak], "run": "r", "check": "c"})
    with pytest.raises(protocol.ProtocolError, match="minProperties|does not have enough properties"):
        protocol.envelope("martin", "RefactorUnit", *CAUSE, {"run": "r", "check": "c"})
    checked = protocol.check_output("martin", "RefactorUnit", out("RefactorUnit", {
        "outcome": "done", "result": {"phase": "strengthen", "addressed": ["test/a_test.exs:3"]}}))
    assert checked.errors == []


LONG = "One. Two. Three."


@pytest.mark.parametrize("agent, command, body, message", [
    ("kent", "ProposeResolution", {"outcome": "done", "result": {
        "subject": "s", "summary": LONG, "changes": "c", "units": []}}, "result.summary: at most 2 sentences"),
    ("kent", "ProposeResolution", {"outcome": "done", "result": {
        "subject": "x" * 73, "summary": "s.", "changes": "c", "units": []}}, "result.subject: at most 72 characters"),
    ("kent", "ProposeResolution", {"outcome": "done", "result": {
        "subject": "One. Two.", "summary": "s.", "changes": "c", "units": []}}, "result.subject: one sentence"),
    ("kent", "ProposeResolution", {"outcome": "done", "result": {"summary": "s.", "changes": "c", "units": []}},
     "'subject' is a required property"),
    ("unclebob", "WriteGreenCode", {"outcome": "change_requested", "change": {
        "kind": "test", "target": "x", "problem": LONG, "proposal": "q"}}, "change.problem: at most 2 sentences"),
    ("kent", "WriteDomain", {"outcome": "question", "question": {"text": "t?", "why": LONG}},
     "question.why: at most 2 sentences"),
])
def test_free_text_is_brief(agent, command, body, message):
    # the user, 2026-11-16: commit messages and the agents' free text in one sentence, two at most
    checked = protocol.check_output(agent, command, out(command, body))
    assert checked.output is None and any(message in e for e in checked.errors), checked.errors
