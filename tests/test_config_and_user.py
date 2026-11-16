"""The configuration's layers and the user's words turned into events."""

import pytest

from beck import config
from beck.events.store import EventStore
from beck.machine import user


def test_the_project_overrides_the_global_file_which_overrides_the_defaults(tmp_path):
    g = tmp_path / "global.yml"
    g.write_text("units_in_parallel: 8\nmodels: {kent: sonnet}\n")
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck/config.yml").write_text("units_in_parallel: 2\n")
    c = config.load(tmp_path, g)
    assert c["units_in_parallel"] == 2
    assert c["models"] == {"kent": "sonnet", "unclebob": "sonnet", "martin": "sonnet", "reader": "haiku",
                           "argyris": "opus"}
    assert c["limits"] == {"times": 3, "attempts": 30}


@pytest.mark.parametrize("text, message", [
    ("units_in_parallel: many\n", "`units_in_parallel` must be a int"),
    ("colour: red\n", "unknown setting `colour`"),
    ("units_in_parallel: 0\n", "at least 1"),
    ("- a list\n", "a mapping is expected"),
])
def test_a_wrong_configuration_is_refused(tmp_path, text, message):
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck/config.yml").write_text(text)
    with pytest.raises(config.ConfigError, match=message):
        config.load(tmp_path, tmp_path / "none.yml")


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "e.db")
    yield s
    s.close()


def command(store, name, **args):
    return store.append("CommandIssued", {"command": name, "target": "user", "args": args})


def test_an_approval_is_yes_or_instructions(store):
    c = command(store, "ApproveDomain", file=".beck/domain.yaml")
    assert [(r.type, r.payload) for r in user.answer(c, "Yes")] == [("DomainApproved", {})]
    assert [(r.type, r.payload) for r in user.answer(c, "Rename it.")] == [("DomainRevised",
                                                                          {"instructions": "Rename it."})]
    review = command(store, "PresentForReview", commits=["a b"])
    assert [(r.type, r.payload) for r in user.answer(review, "fix x")] == [("CorrectionsRequested", {"text": "fix x"})]


def test_the_review_shows_its_notes_before_the_commits(store):
    # the session of 2026-11-16: the notes named three stubs left, after a long list of commits the TUI's main
    # panel cut: the review looked clean
    review = command(store, "PresentForReview", commits=[f"c{i} commit" for i in range(20)],
                     notes=["a stub no cycle implemented: src/a.py:3"])
    lines = user.prompt(review).splitlines()
    assert lines[:3] == ["Review the branch (yes to close the iteration, or the corrections):", "To look at:",
                         "- a stub no cycle implemented: src/a.py:3"]
    assert lines[3] == "c0 commit"


def test_a_choice_must_be_an_option(store):
    c = command(store, "ClassifyProject")
    assert user.answer(c, "Brownfield")[0].payload == {"kind": "brownfield"}
    with pytest.raises(user.DecisionError, match="greenfield, brownfield"):
        user.answer(c, "maybe")


def test_texts_and_confirmations(store):
    q = command(store, "AskInterview", key="goal", question="What?")
    assert user.answer(q, "This.")[0].payload == {"key": "goal", "question": "What?", "answer": "This."}
    b = command(store, "ConfirmBranchName", proposed="calc-add")
    assert user.answer(b, "ok")[0].payload == {"name": "calc-add"}
    assert user.answer(b, "BM-1-calc")[0].payload == {"name": "BM-1-calc"}
    with pytest.raises(user.DecisionError):
        user.answer(b, "   ")
    agent_command = store.append("CommandIssued", {"command": "WriteDomain", "target": "kent", "args": {}})
    with pytest.raises(user.DecisionError, match="not a decision"):
        user.answer(agent_command, "yes")
