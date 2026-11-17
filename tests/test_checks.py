"""beck check: the static checks read deterministically, the agent reads ALL_OK or the findings."""

import json
import sys

import pytest

from beck.checks import Finding, Result, _credo, _mypy, _ruff, _text, summary
from beck.cli import main
from beck.events.project import Project
from beck.toolchain import ToolchainError, load


def test_ruff(tmp_path):
    out = json.dumps([{"filename": str(tmp_path / "src/a.py"), "location": {"row": 3, "column": 5},
                       "code": "F401", "message": "`os` imported but unused"}])
    assert _ruff(out, tmp_path) == [Finding("src/a.py", 3, 5, "F401", "`os` imported but unused")]
    assert _ruff("[]", tmp_path) == []


def test_mypy(tmp_path):
    lines = [json.dumps({"file": "src/a.py", "line": 2, "column": 0, "message": "Incompatible types",
                         "code": "assignment", "severity": "error"}),
             json.dumps({"file": "src/a.py", "line": 2, "column": 0, "message": "see", "code": None,
                         "severity": "note"})]
    assert _mypy("\n".join(lines), tmp_path) == [Finding("src/a.py", 2, 1, "assignment", "Incompatible types")]


def test_credo(tmp_path):
    out = "compiling...\n" + json.dumps({"issues": [{"filename": "lib/a.ex", "line_no": 4, "column": 2,
                                                     "check": "Elixir.Credo.Check.Readability.ModuleDoc",
                                                     "message": "Modules should have a @moduledoc tag."}]})
    assert _credo(out, tmp_path) == [Finding("lib/a.ex", 4, 2, "Readability.ModuleDoc",
                                             "Modules should have a @moduledoc tag.")]


def test_text(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "a.ex").touch()
    out = "noise\nlib/a.ex:10:3: The pattern can never match.\nother.ex:1: not a project file\n"
    assert _text(out, tmp_path) == [Finding("lib/a.ex", 10, 3, "-", "The pattern can never match.")]


def test_the_summary():
    ok = Result("ruff", 0, "l1", [])
    bad = Result("mypy", 1, "l2", [Finding("a.py", 1, 1, "x", "y")])
    lost = Result("dialyzer", 2, "l3", None)
    assert summary([ok]) == "ALL_OK (ruff)"
    assert summary([ok, bad, lost]) == ("NOT_OK mypy (1 findings)\n- a.py:1:1 x y\n"
                                        "NO_PARSE dialyzer (exit 2): its output is in l3")


def test_a_wrong_check_is_refused(tmp_path):
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck" / "toolchain.yml").write_text(
        "toolchain: 1\nlanguage: python\ncheck: [{name: x, command: [x], format: eslint}]\n")
    with pytest.raises(ToolchainError, match="format"):
        load(tmp_path)


FAKE = """import json, sys
paths = sys.argv[1:]
bad = [p for p in paths if 'bad' in p]
print(json.dumps([{"filename": p, "location": {"row": 1, "column": 1}, "code": "E1", "message": "bad"} for p in bad]))
sys.exit(1 if bad else 0)
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / ".beck").mkdir()
    (tmp_path / "fake.py").write_text(FAKE)
    (tmp_path / ".beck" / "toolchain.yml").write_text(
        "toolchain: 1\nlanguage: python\ncheck:\n"
        f"  - {{name: fake, command: [{sys.executable}, fake.py, '{{paths}}'], format: ruff}}\n"
        f"  - {{name: whole, command: [{sys.executable}, -c, 'print(\"lib/x.ex:1: nope\")'], scope: project}}\n")
    return tmp_path


def session(project, monkeypatch):
    from beck.machine.core import Ctx, Engine, Machine
    p = Project(project)
    it, store, started = p.start_iteration()
    Engine(Ctx(store, project, it.id, None), Machine(), None)
    for k, v in (("BECK_CAUSE", started.id), ("BECK_ACTOR", "martin"), ("BECK_PROJECT", str(project))):
        monkeypatch.setenv(k, v)
    return p, it


def test_cli_runs_the_paths_checks_and_records_them(project, capsys, monkeypatch):
    p, it = session(project, monkeypatch)
    assert main(["send", "check", "good.py", "--project", str(project)]) == 0
    assert capsys.readouterr().out.startswith("ALL_OK (fake)\n")
    assert main(["send", "check", "bad.py", "--project", str(project)]) == 1
    assert capsys.readouterr().out.startswith("NOT_OK fake (1 findings)\n- bad.py:1:1 E1 bad")
    e = p.open(it.id).last()
    assert e.type == "ChecksRun" and e.payload["checks"][0]["findings"] == 1 and len(e.payload["checks"]) == 1


def test_cli_all_runs_the_project_checks_too(project, capsys, monkeypatch):
    p, it = session(project, monkeypatch)
    assert main(["send", "check", "--all", "--project", str(project)]) == 0  # exit 0, nothing parsed: ok
    assert capsys.readouterr().out.startswith("ALL_OK (fake, whole)")
    assert [c["name"] for c in p.open(it.id).last().payload["checks"]] == ["fake", "whole"]


def test_cli_needs_paths_a_component_or_all(project):
    with pytest.raises(SystemExit, match="--all"):
        main(["check", "--project", str(project)])


def test_a_component_s_checks_from_its_worktree_read_the_project_s_index(project, capsys, monkeypatch):
    # the session of 2026-11-15: martin ran `beck check --component match-rules` in the component's worktree, which has no
    # .beck/, and beck found no index; the index, like the toolchain, is the project's ($BECK_PROJECT)
    from beck.index.store import add_node, create
    conn = create(project / ".beck/index.db")
    add_node(conn, "shop", "component", namespace=["shop"])
    conn.commit()
    conn.close()
    worktree = project / ".beck/worktrees/shop"
    (worktree / "src/shop").mkdir(parents=True)
    (worktree / "src/shop/__init__.py").write_text("")
    (worktree / "fake.py").write_text(FAKE)
    monkeypatch.setenv("BECK_PROJECT", str(project))
    assert main(["check", "--component", "shop", "--project", str(worktree)]) == 0
    assert capsys.readouterr().out.startswith("ALL_OK (fake)")
