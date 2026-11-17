"""The consumption's projection: the ledger of the turns, the totals per agent and per window, the dashboard."""

import asyncio

import pytest

from beck import config as beck_config
from beck.events.project import Project
from beck.usage import NUMBERS, Usage, window_of


def turn(store, started, agent, command, tokens=(10, 5), ms=2000, model="sonnet", component="u"):
    c = store.append("AgentTurnRequested", {"command": command, "target": agent,
                                       "args": {"session": "s", "context": {},
                                                "component": component}}, cause=started, policy="p")
    store.append("AgentTurnEnded", {"agent": agent, "model": model, "duration_ms": ms,
                                    "tokens": {"input": tokens[0], "output": tokens[1], "cache_read": 100}}, cause=c)
    return c


@pytest.fixture
def recorded(tmp_path):
    project = Project(tmp_path)
    it, store, started = project.start_iteration()
    turn(store, started, "unclebob", "WriteRedTest")
    c = turn(store, started, "martin", "ReviewComponent", tokens=(1, 2), model="opus")
    store.append("SignalRejected", {"agent": "martin", "command": "ReviewComponent", "errors": ["e"], "signature": "x"},
                 cause=c)
    c = store.append("AgentTurnRequested", {"command": "WriteGreenCode", "target": "unclebob",
                                       "args": {"session": "s", "context": {}}}, cause=started, policy="q")
    store.append("AgentTurnFailed", {"agent": "unclebob", "command": "WriteGreenCode", "error": "e",
                                     "signature": "y"}, cause=c)
    return tmp_path, it.id, store


def test_the_turns_are_projected_per_agent_model_and_command(recorded):
    root, it, store = recorded
    usage = Usage(root)
    assert usage.catch_up(store, it) == 4
    rows = {(r["agent"], r["command"]): r for r in usage.by(it, "command")}
    assert rows[("unclebob", "WriteRedTest")]["input"] == 10 and rows[("unclebob", "WriteRedTest")]["ms"] == 2000
    assert rows[("martin", "ReviewComponent")]["turns"] == 1 and rows[("martin", "ReviewComponent")]["rejected"] == 1
    assert rows[("unclebob", "WriteGreenCode")]["failed"] == 1
    models = {(r["agent"], r["model"]) for r in usage.by(it, "agent")}
    assert models == {("unclebob", "sonnet"), ("martin", "opus")}   # a rejected or failed turn: its agent's model
    total = usage.total(it)
    assert (total["turns"], total["failed"], total["rejected"], total["cache_read"]) == (2, 1, 1, 200)


def test_every_number_is_the_sum_of_the_ledger_s_events(recorded):
    root, it, store = recorded
    usage = Usage(root)
    usage.catch_up(store, it)
    ledger = list(usage.events(it))
    assert {r["event_id"] for r in ledger} == {e.id for e in store.read(
        types_=("AgentTurnEnded", "AgentTurnFailed", "SignalRejected"))}
    for n in NUMBERS:
        assert usage.total(it)[n] == sum(r[n] for r in ledger)
        assert sum(r[n] for r in usage.by(it, "window")) == sum(r[n] for r in ledger)
    [martin] = [r for r in usage.by(it, "agent") if r["agent"] == "martin"]
    seqs = [r["seq"] for r in ledger if r["agent"] == "martin"]
    assert (martin["events"], martin["first_seq"], martin["last_seq"]) == (2, min(seqs), max(seqs))
    assert [r["kind"] for r in usage.events(it, agent="martin")] == ["AgentTurnEnded", "SignalRejected"]


def test_catching_up_twice_or_from_two_readers_projects_each_event_once(recorded):
    root, it, store = recorded
    first, second = Usage(root), Usage(root)
    first.catch_up(store, it)
    assert second.catch_up(store, it) == 0 and first.catch_up(store, it) == 0
    started = next(iter(store.read(types_=("SessionStarted",))))
    turn(store, started, "kent", "WriteArchitecture", model="opus")
    assert second.catch_up(store, it) == 1 and first.catch_up(store, it) == 0
    assert first.total(it)["turns"] == 3


def test_a_rebuild_or_another_window_projects_again_the_same(recorded):
    root, it, store = recorded
    usage = Usage(root)
    usage.catch_up(store, it)
    before = usage.by(it, "command")
    assert usage.rebuild(store, it) == 4 and usage.by(it, "command") == before
    five = Usage(root, window_minutes=5)
    assert five.catch_up(store, it) == 4                 # windows of another size: projected again
    assert five.by(it, "command") == before


def test_a_window_starts_at_a_multiple_of_its_minutes():
    assert window_of("2026-11-16T18:09:59.999+00:00", 10) == "2026-11-16T18:00"
    assert window_of("2026-11-16T18:10:00.000+00:00", 10) == "2026-11-16T18:10"
    assert window_of("2026-11-16T18:14:00.000+00:00", 5) == "2026-11-16T18:10"


def test_the_window_must_divide_an_hour(tmp_path):
    (tmp_path / ".beck").mkdir()
    (tmp_path / ".beck/config.yml").write_text("usage: {window_minutes: 7}\n")
    with pytest.raises(beck_config.ConfigError, match="window_minutes"):
        beck_config.load(tmp_path, global_file=tmp_path / "none.yml")
    assert beck_config.load(tmp_path / "x", global_file=tmp_path / "none.yml")["usage"]["window_minutes"] == 10


def test_the_dashboard_attaches_reads_at_each_signal_and_follows_the_log(recorded):
    # the user, 2026-11-17: the dashboard projects nothing; it attaches to the session's socket, learns from the
    # handshake where the read models and the log are, reads them again at each signal, follows the log
    from beck.dashboard import Dashboard
    from beck.socket import Server
    root, it, store = recorded
    usage = Usage(root)
    usage.catch_up(store, it)
    log = root / ".beck" / "beck.log"
    log.write_text("first line\n")

    async def play():
        server = Server(root, lambda m: {"ok": True}, {"iteration": it, "db": str(root / ".beck" / "beck.db"),
                                                       "log": str(log)})
        await server.start()
        app = Dashboard(root)
        async with app.run_test(size=(160, 40)) as pilot:
            for _ in range(50):
                await pilot.pause(0.05)
                if app.redraws:
                    break
            assert "turns 2" in str(app.query_one("#totals").render())
            started = next(iter(store.read(types_=("SessionStarted",))))
            c = store.append("AgentTurnRequested", {"command": "WriteArchitecture", "target": "kent",
                                                    "args": {"session": "k", "context": {}}}, cause=started)
            store.append("AgentTurnEnded", {"agent": "kent", "model": "opus", "tokens": {"input": 1, "output": 1}},
                         cause=c)
            usage.catch_up(store, it)
            with log.open("a") as f:
                f.write("second line\n")
            server.signal()
            for _ in range(50):
                await pilot.pause(0.05)
                if app.redraws > 1 and "second line" in "\n".join(app.query_one("#log").lines):
                    break
            assert "turns 3" in str(app.query_one("#totals").render())
            assert "first line" in app.query_one("#log").lines and "second line" in app.query_one("#log").lines
            await server.stop()
            for _ in range(50):
                await pilot.pause(0.05)
                if app.ended:
                    break
            assert app.ended
    asyncio.run(play())


def test_the_dashboard_needs_attach_and_a_session(tmp_path, capsys):
    from beck.cli import main
    assert main(["dashboard", "--project", str(tmp_path)]) == 2
    assert "--attach" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="no beck session runs"):
        main(["dashboard", "--attach", "--project", str(tmp_path)])


def test_the_engine_projects_after_each_commit_and_a_broken_hook_does_not_stop_it(recorded):
    from beck.machine.core import Ctx, Engine, Machine
    root, it, store = recorded
    usage = Usage(root)
    calls = []

    def broken(new):
        calls.append(new)
        raise RuntimeError("no")

    engine = Engine(Ctx(store, root, it, None), Machine(), None,
                    after_commit=[broken, lambda new: usage.catch_up(store, it)])
    engine.record("SessionAttached", {"from_seq": 0})
    assert [e.type for e in calls[0]] == ["SessionAttached"] and usage.total(it)["turns"] == 2
