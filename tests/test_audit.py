"""smart_audit: the violations a rule decides, from the turn's tool calls, into the outbox."""

import asyncio

from test_cycle import TEST, Agent, git, green, red, repo  # noqa: F401

from beck.cli import main
from beck.events.project import Project
from beck.machine.audit import audit
from beck.machine.core import Ctx, Reactor, Turn
from beck.machine.cycle import Workspace, cycle


class Audited(Agent):
    """Plays unclebob, reporting the tool calls a real session would have made."""

    def __init__(self, root, script, tools):
        super().__init__(root, script)
        self.tools = tools

    async def turn(self, agent, session, envelope, env, cwd):
        turn = await super().turn(agent, session, envelope, env, cwd)
        return Turn(turn.output, turn.model, turn.tokens, None, self.tools.get(envelope["command"], []))


def run(repo, tools):
    agent = Audited(repo, {"WriteRedTest": [red()], "WriteGreenCode": [green()]}, tools)
    project = Project(repo)
    it, store, _ = project.start_iteration()
    workspace = Workspace({"u": repo}, {"u": ["src/pkg"]}, {("u", "operation:double"): {
        "unit": {"id": "u"}, "group": "operation:double", "clauses": [], "entities": [], "dependencies": [],
        "files": {"source": [], "tests": []}}})
    store.append("CycleOpened", {"unit": "u", "clause": "doubles", "group": "operation:double", "first": True},
                 stream="cycle:u/doubles")
    asyncio.run(Reactor(Ctx(store, repo, it.id, workspace), cycle.merge(audit), agent).run_until_idle())
    return project, store


def test_a_clean_turn_logs_nothing(repo):
    project, store = run(repo, {"WriteRedTest": [{"tool": "Read", "input": {"file_path": "src/pkg/mod.py"}},
                                                 {"tool": "Bash", "input": {"command": f"beck test {TEST} --unit u"}}]})
    assert project.pending() == [] and not list(store.read(types_=("ViolationLogged",)))


def test_forbidden_tools_commands_and_exploring_reach_the_outbox(repo):
    project, store = run(repo, {"WriteRedTest": [
        {"tool": "Bash", "input": {"command": "pytest tests"}},
        {"tool": "Bash", "input": {"command": "git commit -am x"}},
        {"tool": "WebFetch", "input": {"url": "https://example.com"}},
        {"tool": "Read", "input": {"file_path": str(repo / "docs/design.md")}},
        {"tool": "Read", "input": {"file_path": "tests/test_mod.py"}},
    ]})
    kinds = sorted(r.kind for r in project.pending())
    assert kinds == ["explored", "forbidden-command", "forbidden-command", "forbidden-tool"]
    explored = next(r for r in project.pending() if r.kind == "explored")
    assert explored.payload == {"agent": "unclebob", "command": "WriteRedTest", "path": "docs/design.md"}
    logged = list(store.read(types_=("ViolationLogged",)))
    ended = store.get(logged[0].causation_id)            # the AuditTurn, caused by the turn's end
    assert store.get(ended.causation_id).type == "AgentTurnEnded"
    assert explored.correlation_id == logged[0].correlation_id


def test_the_outbox_from_the_cli(repo, capsys):
    project, _ = run(repo, {"WriteRedTest": [{"tool": "Bash", "input": {"command": "pytest"}}]})
    assert main(["audit", "--project", str(repo)]) == 0
    line = capsys.readouterr().out.strip()
    assert "forbidden-command" in line and '"input": {"command": "pytest"}' in line
    assert main(["audit", "--handle", project.pending()[0].id, "--project", str(repo)]) == 0
    assert project.pending() == []


def test_martin_touching_the_code_before_the_weak_test_breaks_the_three_laws(tmp_path):
    from beck.events.store import EventStore
    from beck.machine.audit import violations

    store = EventStore(tmp_path / "e.db")
    workspace = Workspace({"u": tmp_path}, {"u": ["src/pkg"]}, {})
    ctx = Ctx(store, tmp_path, "it", workspace)

    def turn(order):
        weak = {"test": "tests/test_a.py::test_a", "clause": "c", "why": "asserts nothing"}
        issued = store.append("CommandIssued", {"command": "RefactorUnit", "target": "martin", "args": {
            "unit": "u", "session": "martin/u", "context": {"weak_tests": [weak], "run": "r", "check": "c"}}})
        for path in order:
            store.append("ToolCalled", {"agent": "martin", "tool": "Edit", "input": {"file_path": path}}, cause=issued)
        return store.append("AgentTurnEnded", {"agent": "martin", "model": "m", "tokens": {"input": 1, "output": 1}},
                            cause=issued)

    assert [k for k, _ in violations(ctx, turn(["src/pkg/a.py", "tests/test_a.py"]))] == ["tdd-order"]
    assert violations(ctx, turn(["tests/test_a.py", "src/pkg/a.py"])) == []
    store.close()
