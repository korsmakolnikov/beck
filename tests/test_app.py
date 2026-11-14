"""beck as an application: start, stop at a decision, answer, attach; interactive on a terminal."""

import io
import json
import stat
import sys

import pytest
import yaml
from test_session import project  # noqa: F401

from beck import app
from beck.cli import main
from beck.events.project import Project

FAKE = r'''#!{python}
import json, sys
from pathlib import Path
envelope = json.loads(sys.stdin.read())
examples = json.loads(Path({examples!r}).read_text())
out = dict(protocol=1, command=envelope["command"], **examples[envelope["agent"]][envelope["command"]]["out"])
if envelope["command"] == "AskFollowUps":
    out["result"] = {{"questions": []}}
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(out),
                  "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
'''


@pytest.fixture
def claude(tmp_path, monkeypatch):
    examples = tmp_path / "examples.json"
    src = __import__("pathlib").Path(__file__).parents[1] / "src/beck/agents/examples.yaml"
    examples.write_text(json.dumps(yaml.safe_load(src.read_text())))
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(python=sys.executable, examples=str(examples)))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BECK_CLAUDE", str(fake))
    return fake


def test_a_batch_start_stops_at_the_first_decision_and_answer_attach_go_on(project, claude, capsys):
    assert main(["start", "--batch", "--project", str(project)]) == 2
    out = capsys.readouterr().out
    iteration = out.split("iteration ")[1].split()[0]
    assert "AskInterview: What must this iteration deliver?" in out
    for _ in range(3):
        assert main(["pending", iteration, "--project", str(project)]) == 0
        decision = capsys.readouterr().out.split("]")[0].lstrip("[")
        assert main(["answer", iteration, decision, "Adding", "numbers.", "--project", str(project)]) == 0
        main(["attach", iteration, "--batch", "--project", str(project)])
        out = capsys.readouterr().out
    assert "ConfirmBranchName: Branch name: BM-12-tagging" in out   # kent's answer, through the driver
    store = Project(project).open(Project(project).find(iteration).id)
    assert [e.type for e in store.read(types_=("SessionAttached",))] == ["SessionAttached"] * 3
    assert any(e.payload["agent"] == "kent" for e in store.read(types_=("AgentTurnEnded",)))


def test_an_interactive_session_asks_on_the_terminal(project, claude):
    words = io.StringIO("Adding.\nmaybe\n" + "Nothing else.\n" * 2)
    out = io.StringIO()
    code = app.run(project, None, interactive=True, read=words, out=out)
    assert code == 2   # the words ran out at the branch's name
    text = out.getvalue()
    assert text.count("AskInterview") == 3 and "ConfirmBranchName" in text
    assert "beck: 'Nothing else.' is not a branch name" in text


def test_a_wrong_answer_is_refused(project, claude, capsys):
    main(["start", "--batch", "--project", str(project)])
    iteration = capsys.readouterr().out.split("iteration ")[1].split()[0]
    assert main(["answer", iteration, "ffffffff", "x", "--project", str(project)]) == 1
    assert "0 pending decisions match" in capsys.readouterr().out
