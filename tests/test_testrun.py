"""beck test makes a test run into evidence: what it records is what the verifiers judge."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from beck.cli import main
from beck.events.project import Project
from beck.testrun import worktree_tree
from beck.toolchain import DEFAULT_CHECKS, DEFAULTS, Toolchain, ToolchainError, load

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={report}", "{tests}"]


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck" / "toolchain.yml").write_text(
        "toolchain: 1\nlanguage: python\ntest: " + repr(PYTEST).replace("'", '"') + "\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_red():\n    # spec: some-clause\n    assert False\n\n\ndef test_green():\n    assert True\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "start")
    for var in ("BECK_ITERATION", "BECK_CAUSE", "BECK_ACTOR"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_the_toolchain_defaults_to_the_language(tmp_path):
    (tmp_path / "mix.exs").touch()
    assert load(tmp_path) == Toolchain("elixir", DEFAULTS["elixir"], check=DEFAULT_CHECKS["elixir"],
                                       build=["mix", "compile"], complexity="credo",
                                       deps=["mix", "deps.get"])


@pytest.mark.parametrize("text, message", [
    ("language: python\n", "toolchain: 1"),
    ("toolchain: 1\nlanguage: rust\n", "language"),
    ("toolchain: 1\nlanguage: python\nexec: docker\n", "exec"),
])
def test_a_wrong_toolchain_is_refused(tmp_path, text, message):
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck" / "toolchain.yml").write_text(text)
    with pytest.raises(ToolchainError, match=message):
        load(tmp_path)


def test_the_test_command_expands_tests_report_exec_and_env():
    t = Toolchain("python", ["pytest", "--junitxml={report}", "{tests}"], env={"A": "1"})
    assert t.test_argv(["t1", "t2"], "r.xml") == (["pytest", "--junitxml=r.xml", "t1", "t2"],
                                                 {"A": "1", "BECK_REPORT": "r.xml"})
    wrapped = Toolchain("elixir", ["mix", "test", "{tests}"], exec=["docker", "exec", "app"])
    assert wrapped.test_argv([], "r.xml") == (["docker", "exec", "app", "env", "BECK_REPORT=r.xml", "mix", "test"], {})


def test_selectors():
    assert Toolchain("python", []).selector("tests/t.py", "TestA.test_b", 3) == "tests/t.py::TestA::test_b"
    assert Toolchain("elixir", []).selector("test/t_test.exs", "x", 7) == "test/t_test.exs:7"


def test_a_run_is_recorded_with_its_evidence(project, capsys, monkeypatch):
    p = Project(project)
    it, store, started = p.start_iteration()
    store.close()
    monkeypatch.setenv("BECK_CAUSE", started.id)
    monkeypatch.setenv("BECK_ACTOR", "unclebob")
    code = main(["test", "tests/test_a.py::test_red", "--unit", "u", "--clause", "some-clause",
                 "--project", str(project)])
    assert code == 1
    out = capsys.readouterr().out
    assert out.startswith("NOT_OK (1 of 1 not passed)\n- tests/test_a.py::test_red [failure] at tests/test_a.py:3")
    assert "assert False" in out and "pytest" not in out.split("(TestsRun")[0]
    e = p.open(it.id).last()
    assert e.type == "TestsRun" and e.actor == "unclebob" and e.stream == "cycle:u/some-clause"
    assert e.causation_id == started.id and e.correlation_id == started.id
    assert e.payload["exit_code"] == 1 and e.payload["tests"] == ["tests/test_a.py::test_red"]
    report = project / e.payload["report"]
    assert hashlib.sha256(report.read_bytes()).hexdigest() == e.payload["report_hash"]
    assert 'name="test_red"' in report.read_text()
    assert e.payload["tree"] == worktree_tree(project)
    assert e.payload["counts"] == {"tests": 1, "passed": 0, "failure": 1, "error": 0, "skipped": 0}
    assert "test_red" in (project / e.payload["log"]).read_text()
    assert e.sha is not None


def test_the_tree_changes_with_the_files_but_not_with_beck_dir(project):
    before = worktree_tree(project)
    (project / ".beck" / "anything").write_text("x")
    assert worktree_tree(project) == before
    (project / "tests" / "test_a.py").write_text("changed\n")
    assert worktree_tree(project) != before


def test_a_run_without_report_is_still_recorded(project):
    (project / ".beck" / "toolchain.yml").write_text("toolchain: 1\nlanguage: python\ntest: [\"true\"]\n")
    p = Project(project)
    it, store, _ = p.start_iteration()
    store.close()
    assert main(["test", "--project", str(project)]) == 0
    e = p.open(it.id).last()
    assert e.payload["exit_code"] == 0 and "report" not in e.payload and e.stream == "session"


def test_the_index_selects_the_tests_of_a_clause(project, capsys):
    Project(project).start_iteration()[1].close()
    examples = Path(__file__).parents[1] / "docs" / "examples" / "bookmarks"
    for name in ("domain.yaml", "architecture.yaml"):
        (project / ".beck" / name).write_text((examples / name).read_text())
    (project / "tests" / "test_a.py").write_text(
        "def test_name_length():\n    # spec: tag-name-is-1-to-40-characters\n    assert True\n")
    assert main(["index", "build", "--project", str(project)]) == 0
    assert main(["test", "--clause", "tag-name-is-1-to-40-characters", "--project", str(project)]) == 0
    assert "ALL_OK (1 passed)" in capsys.readouterr().out
    assert Project(project).open(Project(project).iterations()[0].id).last().payload["tests"] == [
        "tests/test_a.py::test_name_length"]


def test_no_open_iteration_is_an_error(project):
    p = Project(project)
    it, store, _ = p.start_iteration()
    store.close()
    p.end_iteration(it.id)
    with pytest.raises(SystemExit, match="no open iteration"):
        main(["test", "--project", str(project)])


def test_a_run_without_report_tells_the_agent_where_the_log_is(project, capsys):
    (project / ".beck" / "toolchain.yml").write_text("toolchain: 1\nlanguage: python\ntest: [\"true\"]\n")
    Project(project).start_iteration()[1].close()
    main(["test", "--project", str(project)])
    assert capsys.readouterr().out.startswith("NO_REPORT (exit 0): the runner wrote no JUnit report; "
                                              "its output is in .beck/reports/")
