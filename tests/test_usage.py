"""The consumption's projection: the ledger of the turns, the totals per agent and per window, the dashboard."""

import io

import pytest
from rich.console import Console

from beck import config as beck_config
from beck.dashboard import render
from beck.events.project import Project
from beck.usage import NUMBERS, Usage, window_of


def turn(store, started, agent, command, tokens=(10, 5), ms=2000, model="sonnet", unit="u"):
    c = store.append("CommandIssued", {"command": command, "target": agent,
                                       "args": {"session": "s", "context": {}, "unit": unit}}, cause=started, policy="p")
    store.append("AgentTurnEnded", {"agent": agent, "model": model, "duration_ms": ms,
                                    "tokens": {"input": tokens[0], "output": tokens[1], "cache_read": 100}}, cause=c)
    return c


@pytest.fixture
def recorded(tmp_path):
    project = Project(tmp_path)
    it, store, started = project.start_iteration()
    turn(store, started, "unclebob", "WriteRedTest")
    c = turn(store, started, "martin", "ReviewUnit", tokens=(1, 2), model="opus")
    store.append("SignalRejected", {"agent": "martin", "command": "ReviewUnit", "errors": ["e"], "signature": "x"},
                 cause=c)
    c = store.append("CommandIssued", {"command": "WriteGreenCode", "target": "unclebob",
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
    assert rows[("martin", "ReviewUnit")]["turns"] == 1 and rows[("martin", "ReviewUnit")]["rejected"] == 1
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


def test_the_dashboard_shows_the_agents_the_windows_and_the_ledger(recorded):
    root, it, store = recorded
    usage = Usage(root)
    usage.catch_up(store, it)
    out = io.StringIO()
    Console(file=out, width=140).print(render(usage, store, it, "branch", 140))
    text = out.getvalue()
    assert "unclebob" in text and "martin" in text and "tokens per 10 minutes" in text
    assert "rejected" in text and "failed" in text
    last = list(usage.events(it))[-1]
    assert last["event_id"][:8] in text


def test_the_reactor_projects_and_a_broken_projector_does_not_stop_it(recorded):
    from beck.machine.core import Ctx, Machine, Reactor
    root, it, store = recorded
    usage = Usage(root)
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("no")

    reactor = Reactor(Ctx(store, root, it, None), Machine(), None,
                      projectors=[broken, lambda: usage.catch_up(store, it)])
    reactor.react()
    assert calls and usage.total(it)["turns"] == 2
