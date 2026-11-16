"""A whole iteration, from SessionStarted to IterationClosed, on a real brownfield project with scripted agents."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from beck.events.project import Project
from beck.machine import user
from beck.machine.core import Ctx, Reactor, Turn
from beck.machine.session import IndexWorkspace, beck_machine, classify_project
from beck.testrun import run as run_tests
from beck.toolchain import load

PYTEST = f'[{sys.executable}, -m, pytest, -q, -p, no:cacheprovider, "--junitxml={{report}}", "{{tests}}"]'
DOMAIN = """domain: 1
system: calc
entities:
  - name: Number
    kind: value_object
    description: An integer.
    fields: [{name: value, type: integer}]
morphisms:
  - id: add
    description: Two numbers are added.
    uses: [Number]
    post:
      - id: add-sums
        rule: The result is the sum of the two numbers.
"""
ARCHITECTURE = """architecture: 1
system: {id: calc, name: Calc, physical: calc}
elements:
  - kind: component
    id: adder
    name: Adder
    physical: adder
    boundary: concrete
    implements: [add]
"""
INTERFACE = 'def add(a, b):\n    """Adds two numbers. spec: add"""\n    raise NotImplementedError\n'


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    (tmp_path / "src/calc").mkdir(parents=True)
    (tmp_path / "src/calc/__init__.py").write_text('"""A calculator."""\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_existing.py").write_text("def test_it_imports():\n    import calc  # noqa\n")
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck/toolchain.yml").write_text(
        f"toolchain: 1\nlanguage: python\ntest: {PYTEST}\ncheck: []\n"
        f"build: [{sys.executable}, -m, compileall, -q, src]\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "beck@example.com")
    git(tmp_path, "config", "user.name", "beck")
    Project(tmp_path)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "start")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    for var in ("BECK_CAUSE", "BECK_ITERATION", "BECK_ACTOR", "BECK_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def done(command, **result):
    body = {"protocol": 1, "command": command, "outcome": "done"}
    if result:
        body["result"] = result
    return body


class Agents:
    def __init__(self, project, domain=(DOMAIN,), shopping_questions=()):
        self.project, self.domains, self.calls = project, list(domain), []
        self.shopping_questions, self.shopping_file = list(shopping_questions), None

    async def turn(self, agent, session, envelope, env, cwd):
        self.calls.append((agent, envelope))
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return Turn(getattr(self, envelope["command"])(envelope, Path(cwd)), "fake", {"input": 1, "output": 1})
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    # kent
    def WireCoverage(self, e, cwd):
        assert e["context"]["manifest"] == "pyproject.toml"
        toolchain = cwd / e["context"]["toolchain"]
        toolchain.write_text(toolchain.read_text() + 'coverage: ["--cov={path}", "--cov-report=json:{coverage}"]\n')
        return done("WireCoverage", tool="pytest-cov", summary="pytest-cov, already a dependency, wired.")

    def AskFollowUps(self, e, cwd):
        return done("AskFollowUps", questions=[])

    def ProposeBranchName(self, e, cwd):
        return done("ProposeBranchName", name="calc-add")

    def DelimitCodeArea(self, e, cwd):
        return done("DelimitCodeArea", paths=["src/calc"])

    def WriteShoppingList(self, e, cwd):
        self.shopping_file = e["context"].get("file") or self.shopping_file  # a real session remembers it
        if self.shopping_questions:  # a question first: the turn resumes with the answer, without its context
            return {"protocol": 1, "command": "WriteShoppingList", "outcome": "question",
                    "question": {"text": self.shopping_questions.pop(0), "why": "The list depends on it."}}
        (cwd / self.shopping_file).write_text("# calc-add\n\n- add two numbers\n")
        return done("WriteShoppingList")

    def WriteDomain(self, e, cwd):
        text = self.domains.pop(0) if len(self.domains) > 1 else self.domains[0]
        (cwd / ".beck/domain.yaml").write_text(text)
        return done("WriteDomain")

    def WriteArchitecture(self, e, cwd):
        (cwd / ".beck/architecture.yaml").write_text(ARCHITECTURE)
        return done("WriteArchitecture")

    def WriteInterfaces(self, e, cwd):
        (cwd / "src/calc/adder").mkdir(exist_ok=True)
        (cwd / "src/calc/adder/__init__.py").write_text(INTERFACE)
        return done("WriteInterfaces", files=["src/calc/adder/__init__.py"])

    # unclebob
    def WriteRedTest(self, e, cwd):
        (cwd / "tests/test_adder.py").write_text(
            "from calc.adder import add\n\n\ndef test_add_sums():\n    # spec: add-sums\n    assert add(2, 3) == 5\n")
        self._test(cwd)
        return done("WriteRedTest", test="tests/test_adder.py::test_add_sums")

    def WriteGreenCode(self, e, cwd):
        (cwd / "src/calc/adder/__init__.py").write_text(INTERFACE.replace("raise NotImplementedError", "return a + b"))
        self._test(cwd)
        return done("WriteGreenCode")

    def _test(self, cwd):
        run_tests(cwd, load(self.project), ["tests/test_adder.py::test_add_sums"], actor="unclebob", component="adder",
                  requirement="add-sums", store_root=self.project)

    # martin
    def ReviewComponent(self, e, cwd):
        return done("ReviewComponent", findings=[], weak_tests=[])


ANSWERS = {"AskInterview": "Adding two numbers.", "ConfirmBranchName": "yes", "ApproveShoppingList": "yes",
           "ApproveDomain": "yes", "ApproveArchitecture": "yes", "PresentForReview": "yes"}


def drive(reactor, answers, limit=40):
    """Runs the machine, answering the user's commands from the script, until nothing is left to answer."""
    asked = []
    for _ in range(limit):
        asyncio.run(reactor.run_until_idle())
        pending = [c for c in reactor.pending() if c.payload["target"] == "user"]
        if not pending:
            return asked
        command = pending[0]
        name = command.payload["command"]
        asked.append((name, user.prompt(command)))
        if name not in answers:
            return asked
        words = answers[name]
        if isinstance(words, list):  # one answer per time, the last one repeating
            words = words.pop(0) if len(words) > 1 else words[0]
        reactor.answer(command.id, user.answer(command, words))
    raise AssertionError("the session does not end")


def start(project, agents):
    p = Project(project)
    it, store, _ = p.start_iteration()
    ctx = Ctx(store, project, it.id, IndexWorkspace(store, project))
    return Reactor(ctx, beck_machine, agents), store, p, it


def test_a_whole_iteration_from_the_interview_to_the_close(project):
    agents = Agents(project)
    reactor, store, p, it = start(project, agents)
    asked = drive(reactor, ANSWERS)
    assert [a for a, _ in asked] == ["AskInterview"] * 3 + ["ConfirmBranchName", "ApproveShoppingList",
                                                             "ApproveDomain", "ApproveArchitecture",
                                                             "PresentForReview"]
    assert [e.payload["tool"] for e in store.read(types_=("CoverageWired",))] == ["pytest-cov"]
    baseline = next(e for e in store.read(types_=("CoverageMeasured",)) if "component" not in e.payload)
    assert "src/calc/__init__.py" in baseline.payload["files"]
    assert [e.type for e in store.read(types_=("IterationClosed",))] == ["IterationClosed"]
    assert p.iteration(it.id).name == "calc-add" and p.iteration(it.id).ended_at
    assert git(project, "rev-parse", "--abbrev-ref", "HEAD") == "calc-add"
    assert git(project, "log", "--format=%s").splitlines() == [
        "feat(adder): implement adder", "feat: the interfaces and the fitness functions of calc-add", "start"]
    assert "return a + b" in (project / "src/calc/adder/__init__.py").read_text()
    assert (project / "tests/fitness/test_architecture.py").is_file()
    red = next(e for a, e in agents.calls if e["command"] == "WriteRedTest")
    assert red["context"]["unit"]["morphism"]["id"] == "add"
    assert red["context"]["unit"]["morphism"]["symbols"][0]["file"] == "src/calc/adder/__init__.py"
    # every event of the iteration descends from its SessionStarted
    started = list(store.read(types_=("SessionStarted",)))[0]
    assert all(e.correlation_id == started.id for e in store.read() if e.causation_id)
    assert not [c for c in reactor.pending()]


def test_a_shopping_list_written_after_the_questions_of_kent_is_approved(project):
    # the user's session of 2026-11-14 (events 75-91): two questions, then the list; beck died on the answer
    agents = Agents(project, shopping_questions=["Which library for the TUI: curses, blessed, rich?",
                                                 "By \"the 2\" do you mean blessed?"])
    reactor, store, *_ = start(project, agents)
    asked = drive(reactor, dict(ANSWERS, Ask=["Ok the 2", "yes blessed"]))
    assert [a for a, _ in asked][3:7] == ["ConfirmBranchName", "Ask", "Ask", "ApproveShoppingList"]
    [written] = store.read(types_=("ShoppingListWritten",))
    assert written.payload["file"] == ".beck/shopping_lists/calc-add.md"
    assert list(store.read(types_=("IterationClosed",)))


def test_an_invalid_domain_goes_back_to_kent_with_the_errors(project):
    agents = Agents(project, domain=(DOMAIN.replace("uses: [Number]", "uses: [Numbr]"), DOMAIN))
    reactor, store, *_ = start(project, agents)
    drive(reactor, ANSWERS)
    [invalid] = store.read(types_=("DomainInvalid",))
    assert invalid.payload["errors"] == ["morphisms[0].uses[0]: no entity named 'Numbr'"]
    second = [e for a, e in agents.calls if e["command"] == "WriteDomain"][1]
    assert second["context"]["errors"] == invalid.payload["errors"]
    assert list(store.read(types_=("IterationClosed",)))


def test_a_domain_that_stays_invalid_asks_the_user(project):
    agents = Agents(project, domain=(DOMAIN.replace("uses: [Number]", "uses: [Numbr]"),))
    reactor, store, *_ = start(project, agents)
    asked = drive(reactor, ANSWERS)
    assert asked[-1][0] == "Ask" and asked[-1][1].startswith("kent's domain is blocked: the same result 3 times")
    assert len(list(store.read(types_=("DomainInvalid",)))) == 3


def test_a_revision_of_the_user_reaches_kent(project):
    agents = Agents(project)
    reactor, store, *_ = start(project, agents)
    answers = dict(ANSWERS, ApproveDomain=["Name the entity Integer.", "yes"])
    drive(reactor, answers)
    revisions = [e["context"].get("revision") for a, e in agents.calls if e["command"] == "WriteDomain"]
    assert revisions == [None, "Name the entity Integer."]
    assert [e.type for e in store.read(types_=("DomainRevised", "IterationClosed"))] == ["DomainRevised",
                                                                                         "IterationClosed"]


def test_the_classification(tmp_path):
    assert classify_project(tmp_path) == "greenfield"
    (tmp_path / "notes.md").write_text("x")
    assert classify_project(tmp_path) == "undetermined"
    (tmp_path / "a.py").write_text("x")
    assert classify_project(tmp_path) == "brownfield"


def test_the_language_is_asked_open_and_one_beck_cannot_lay_is_asked_again(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    for var, value in (("GIT_AUTHOR_NAME", "beck"), ("GIT_AUTHOR_EMAIL", "beck@example.com"),
                       ("GIT_COMMITTER_NAME", "beck"), ("GIT_COMMITTER_EMAIL", "beck@example.com")):
        monkeypatch.setenv(var, value)
    project = tmp_path / "app"
    project.mkdir()
    reactor, store, *_ = start(project, Agents(project))
    asked = drive(reactor, {"AskInterview": ["A game.", "Nothing.", "It runs.", "Rust", " Python ", "app"]})
    questions = [q for a, q in asked if a == "AskInterview"]
    assert questions[3] == "Which language?"
    assert questions[4].startswith("beck cannot lay a 'rust' project yet: it lays python, elixir.")
    assert questions[5] == "The project's name?"
    assert [e.payload["language"] for e in store.read(types_=("ProjectScaffolded",))] == ["python"]


def test_the_workspace_plans_the_components_again_from_the_model_of_now(project):
    (project / ".beck/domain.yaml").write_text(DOMAIN.replace("""
      - id: add-sums
        rule: The result is the sum of the two numbers.
""", """
      - id: add-sums
        rule: The result is the sum of the two numbers.
      - id: add-commutes
        rule: The order of the numbers does not change the sum.
"""))
    (project / ".beck/architecture.yaml").write_text(ARCHITECTURE)
    store = Project(project).start_iteration()[1]
    workspace = IndexWorkspace(store, project)
    requirements = lambda scheduled: [c for u in scheduled["components"] for g in u["units"] for c in g["requirements"]]  # noqa: E731
    assert "add-commutes" in requirements(workspace.rederive())
    (project / ".beck/domain.yaml").write_text(DOMAIN)
    assert "add-commutes" not in requirements(workspace.rederive())


def test_a_component_s_tests_are_found_in_its_worktree_with_the_project_s_model(project, monkeypatch, capsys):
    # the session of 2026-11-15: martin's `beck test --component match-rules`, in the component's worktree, got "the index
    # knows no test for that": the project's index is built on the main tree, which has not the component's tests yet
    from beck.cli import main
    (project / ".beck/domain.yaml").write_text(DOMAIN)
    (project / ".beck/architecture.yaml").write_text(ARCHITECTURE)
    (project / "src/calc/adder").mkdir()
    (project / "src/calc/adder/__init__.py").write_text(INTERFACE)
    assert main(["index", "build", "--project", str(project)]) == 0
    worktree = project / ".beck/worktrees/adder"
    git(project, "worktree", "add", "-q", "-b", "component-adder", str(worktree), "HEAD")   # as StartComponent lays it
    (worktree / "src/calc/adder").mkdir()
    (worktree / "src/calc/adder/__init__.py").write_text(INTERFACE.replace("raise NotImplementedError", "return a + b"))
    (worktree / "tests/test_adder.py").write_text(
        "from calc.adder import add\n\n\ndef test_add_sums():\n    # spec: add-sums\n    assert add(2, 3) == 5\n")
    Project(project).start_iteration()
    monkeypatch.setenv("BECK_PROJECT", str(project))
    capsys.readouterr()
    assert main(["test", "--component", "adder", "--project", str(worktree)]) == 0
    assert "1 passed" in capsys.readouterr().out


def test_the_index_tells_a_requirement_an_earlier_green_implemented(project):
    # the user, 2026-11-16: the index must say which code covers a requirement; a red that passes on it is covered
    (project / ".beck/domain.yaml").write_text(DOMAIN)
    (project / ".beck/architecture.yaml").write_text(ARCHITECTURE)
    worktree = project / ".beck/worktrees/adder"
    git(project, "worktree", "add", "-q", "-b", "component-adder", str(worktree), "HEAD")
    (worktree / "src/calc/adder").mkdir()
    (worktree / "src/calc/adder/__init__.py").write_text(INTERFACE)
    store = Project(project).start_iteration()[1]
    store.append("ComponentStarted", {"component": "adder", "worktree": str(worktree), "branch": "component-adder", "base": "x"},
                 stream="component:adder")
    workspace = IndexWorkspace(store, project)
    assert not workspace.already_met("adder", "add-sums")                  # the interface is still an unimplemented
    (worktree / "src/calc/adder/__init__.py").write_text(INTERFACE.replace("raise NotImplementedError", "return a + b"))
    assert workspace.already_met("adder", "add-sums")


class Unreferenced(Agents):
    """Writes, first, an interface with an unimplemented no id references, as kent wrote main() and SystemClock.sleep."""

    def WriteInterfaces(self, e, cwd):
        first = not [1 for a, c in self.calls if c["command"] == "WriteInterfaces"][1:]
        done_ = super().WriteInterfaces(e, cwd)
        if first:
            path = cwd / "src/calc/adder/__init__.py"
            path.write_text(path.read_text() + '\n\ndef helper():\n    """Helps."""\n    raise NotImplementedError\n')
        return done_


def test_an_unimplemented_no_id_references_sends_the_interfaces_back_to_kent(project):
    # the sessions of 2026-11-15 and 16: main(), and the clock's real implementation, referenced no id: no cycle
    # implemented them, the suite stayed green and the program did not start
    agents = Unreferenced(project)
    reactor, store, *_ = start(project, agents)
    drive(reactor, ANSWERS)
    [broken] = store.read(types_=("InterfacesBroken",))
    assert broken.payload["errors"] == ["src/calc/adder/__init__.py:8: an unimplemented no id references"]
    second = [e for a, e in agents.calls if e["command"] == "WriteInterfaces"][1]
    assert second["context"]["errors"] == broken.payload["errors"]
    assert list(store.read(types_=("IterationClosed",)))


def test_a_reference_to_what_another_component_implements_sends_the_interfaces_back(project):
    # the replay of 2026-11-16: program's main referenced an error of setup's start-game; program had no cycle
    # for it, setup's cycle does not write program's code, and program stopped
    from beck.machine.core import Ctx
    from beck.machine.session import foreign_references
    (project / ".beck/domain.yaml").write_text(DOMAIN + """  - id: show
    description: A number is shown.
    uses: [Number]
    post:
      - id: show-prints
        rule: The number is printed.
""")
    (project / ".beck/architecture.yaml").write_text(ARCHITECTURE + """  - kind: component
    id: display
    name: Display
    physical: display
    boundary: concrete
    depends_on: [adder]
    implements: [show]
""")
    (project / "src/calc/adder").mkdir()
    (project / "src/calc/adder/__init__.py").write_text(INTERFACE)
    (project / "src/calc/display").mkdir()
    (project / "src/calc/display/__init__.py").write_text(
        'def show(n):\n    """Shows a number. spec: show"""\n    raise NotImplementedError\n\n\n'
        'def main():\n    """Adds and shows. spec: add-sums"""\n    raise NotImplementedError\n')
    ctx = Ctx(None, project, "it", None)
    assert foreign_references(ctx, "python") == [
        "src/calc/display/__init__.py:7: display references add-sums, which adder implements: "
        "reference what display implements"]
    (project / "src/calc/display/__init__.py").write_text(
        'def show(n):\n    """Shows a number. spec: show"""\n    raise NotImplementedError\n')
    assert foreign_references(ctx, "python") == []
