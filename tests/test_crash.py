"""beck's own exceptions: recorded as BeckCrashed, sent to the outbox for argyris, resumed after the fix.

The user's session of 2026-11-14 died in a translator (KeyError) inside the
TUI's worker: no event recorded it, so smart_audit had nothing to post and
argyris nothing to read.
"""

import asyncio

from test_session import ANSWERS, Agents, drive, project, start  # noqa: F401

from beck.app import reactor_for
from beck.events.project import Project
from beck.machine.core import Ctx, Reactor
from beck.machine.session import IndexWorkspace, beck_machine
from beck.tui import BeckApp


def boom(*args):
    raise KeyError("context")


def resume(project, store, it):
    """A new reactor on the same store, as `beck attach` makes one after the fix."""
    reactor = Reactor(Ctx(store, project, it.id, IndexWorkspace(store, project)), beck_machine, Agents(project))
    drive(reactor, ANSWERS)
    return reactor


def crashed(store):
    return [e.payload for e in store.read(types_=("BeckCrashed",))]


def test_a_translator_that_raises_keeps_the_turn_and_leaves_the_command_pending(project):
    reactor, store, p, it = start(project, Agents(project))
    reactor.machine.translators["WriteShoppingList"] = boom
    drive(reactor, ANSWERS)                                   # returns: the session does not die
    [crash] = crashed(store)
    assert crash["where"] == "translator" and crash["name"] == "WriteShoppingList"
    assert crash["error"] == "KeyError: 'context'" and "boom" in crash["traceback"]
    turn = next(e for e in store.read(types_=("CommandIssued",)) if e.payload["command"] == "WriteShoppingList")
    # its tokens and its changes kept, not done
    assert [e.type for e in store.caused_by(turn.id)] == ["AgentTurnEnded", "TurnChanged", "BeckCrashed"]
    assert [r.kind for r in p.pending()] == ["beck-crashed"]
    assert p.pending()[0].payload["name"] == "WriteShoppingList"
    resume(project, store, it)
    assert list(store.read(types_=("IterationClosed",)))


def test_a_handler_that_raises_is_run_again_on_resume(project):
    reactor, store, p, it = start(project, Agents(project))
    reactor.machine.handlers["CreateBranch"] = boom
    drive(reactor, ANSWERS)
    assert [(c["where"], c["name"]) for c in crashed(store)] == [("handler", "CreateBranch")]
    assert not list(store.read(types_=("BranchCreated",)))
    resume(project, store, it)
    assert [e.type for e in store.read(types_=("BranchCreated", "IterationClosed"))] == ["BranchCreated",
                                                                                        "IterationClosed"]


def test_a_policy_that_raises_is_run_again_on_resume(project):
    reactor, store, p, it = start(project, Agents(project))
    policies = reactor.machine.policies["BranchCreated"]
    reactor.machine.policies["BranchCreated"] = [(name, boom) for name, _ in policies]
    drive(reactor, ANSWERS)
    assert {(c["where"], c["name"]) for c in crashed(store)} == {("policy", name) for name, _ in policies}
    assert [r.kind for r in p.pending()] == ["beck-crashed"] * len(policies)
    resume(project, store, it)
    assert list(store.read(types_=("IterationClosed",)))


def test_the_tui_shows_the_crash_instead_of_dying(project):
    p = Project(project)
    it, store, _ = p.start_iteration()
    reactor = reactor_for(p, it.id, store, Agents(project))
    reactor.machine.translators["WriteShoppingList"] = boom
    app = BeckApp(reactor)

    async def play():
        async with app.run_test() as pilot:
            for _ in range(200):
                await pilot.pause(0.05)
                if app.result == 1:
                    break
                if app.waiting is not None:
                    app.query_one("#answer").value = ANSWERS[app.waiting.payload["command"]]
                    await pilot.press("enter")
            main = str(app.query_one("#main").render())
            await pilot.press("ctrl+c")
            return main

    main = asyncio.run(play())
    assert app.result == 1
    assert "beck crashed" in main and "WriteShoppingList" in main and f"beck tui {it.id[:8]}" in main
