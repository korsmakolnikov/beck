"""The TUI and its views, on a whole scripted iteration."""

import asyncio

from test_session import ANSWERS, Agents, drive, project, start  # noqa: F401

from beck import views
from beck.app import reactor_for
from beck.events.project import Project
from beck.tui import BeckApp


def test_the_views_of_a_whole_iteration(project):
    reactor, store, *_ = start(project, Agents(project))
    drive(reactor, ANSWERS)
    rows = {t.agent: t for t in views.tokens(store)}
    assert set(rows) == {"kent", "unclebob", "martin"} and rows["unclebob"].turns == 2
    assert views.units(store) == [("adder", "done")]
    assert views.graph(store) == ["■ adder                done        "]
    assert views.phase(store) == "closed" and views.running(store) == []
    assert any("IterationClosed" in line for line in views.activity(store))
    assert views.histogram(views.tokens(store), 10)[0].startswith("kent      █")


def test_the_tui_asks_the_decisions_and_runs_to_the_close(project):
    p = Project(project)
    it, store, _ = p.start_iteration()
    app = BeckApp(reactor_for(p, it.id, store, Agents(project)))

    async def play():
        async with app.run_test() as pilot:
            for _ in range(200):
                await pilot.pause(0.05)
                if app.result == 0:
                    break
                if app.waiting is not None:
                    name = app.waiting.payload["command"]
                    app.query_one("#answer").value = ANSWERS[name]
                    await pilot.press("enter")
            app.refresh_panels()
            status = str(app.query_one("#status").render())
            await pilot.press("ctrl+c")
            return status

    status = asyncio.run(play())
    assert app.result == 0 and "phase: closed" in status
    assert list(store.read(types_=("IterationClosed",)))
