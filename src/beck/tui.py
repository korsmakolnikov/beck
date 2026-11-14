"""The TUI (Textual), in tmux-like areas, around the reactor (README, "TUI").

    ┌ status: phase · tokens · active agents ──────────────────────────────────┐
    │ activity (the event stream, summarised)   │ units (the graph, by state)  │
    │                                           │ tokens per turn (histogram)  │
    ├ main: the decision waiting for the user ───────────────────────────────────┤
    │ > the answer                                                               │
    └────────────────────────────────────────────────────────────────────────────┘

The reactor runs as a worker; every few hundred milliseconds the panels are
folded again from the events. A decision of the user waits in the main
panel; the answer is typed below it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, Static

from beck import views
from beck.events.project import Project
from beck.events.store import Event
from beck.machine import user
from beck.machine.core import Agents, Reactor


class BeckApp(App[int]):
    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #top { height: 1fr; }
    #activity { width: 2fr; border: round $primary; padding: 0 1; }
    #side { width: 1fr; }
    #units, #tokens { border: round $secondary; padding: 0 1; height: 1fr; }
    #main { height: auto; max-height: 12; border: round $accent; padding: 0 1; }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit (safe: beck attach resumes)")]

    def __init__(self, reactor: Reactor):
        super().__init__()
        self.reactor = reactor
        self.store = reactor.ctx.store
        self.waiting: Event | None = None
        self.result = 2

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="top"):
            yield Static(id="activity")
            with Vertical(id="side"):
                yield Static(id="units")
                yield Static(id="tokens")
        yield Static(id="main")
        yield Input(placeholder="the answer: yes, or the words", id="answer")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"beck · {self.reactor.ctx.iteration[:8]}"
        self.set_interval(0.3, self.refresh_panels)
        self.run_worker(self.run_machine(), exclusive=True)

    async def run_machine(self) -> None:
        while True:
            await self.reactor.run_until_idle()
            end = next(iter(self.store.read(types_=("IterationClosed", "ConfigInvalid"))), None)
            if end is not None:
                self.result = 0 if end.type == "IterationClosed" else 1
                self.query_one("#main", Static).update(f"beck: {end.type}. Ctrl-C to quit.")
                return
            decisions = [c for c in self.reactor.pending() if c.payload["target"] == "user"]
            if not decisions:
                self.query_one("#main", Static).update("Nothing to do and nothing to answer: see `beck events`.")
                return
            self.waiting = decisions[0]
            self.query_one("#main", Static).update(
                f"[b]{self.waiting.payload['command']}[/b]  {user.prompt(self.waiting)}")
            self.query_one("#answer", Input).focus()
            while self.waiting is not None:
                await asyncio.sleep(0.1)

    def on_input_submitted(self, message: Input.Submitted) -> None:
        if self.waiting is None or not message.value.strip():
            return
        try:
            self.reactor.answer(self.waiting.id, user.answer(self.waiting, message.value))
        except user.DecisionError as error:
            self.query_one("#main", Static).update(f"{user.prompt(self.waiting)}\n[red]{error}[/red]")
            return
        message.input.value = ""
        self.waiting = None
        self.query_one("#main", Static).update("…")

    def refresh_panels(self) -> None:
        active = views.running(self.store)
        agents = ", ".join(sorted({c.payload["target"] for c in active})) or "none"
        self.query_one("#status", Static).update(
            f"phase: {views.phase(self.store)}   tokens: {views.total_tokens(self.store):,}   active: {agents}")
        self.query_one("#activity", Static).update("\n".join(views.activity(self.store, 60)))
        self.query_one("#units", Static).update("[b]units[/b]\n" + ("\n".join(views.graph(self.store)) or "none yet"))
        self.query_one("#tokens", Static).update(
            "[b]tokens per turn[/b]\n" + ("\n".join(views.histogram(views.tokens(self.store), 18)) or "none yet"))


def run(project_root: Path, iteration: str | None, agents: Agents | None = None) -> int:
    from beck.app import reactor_for
    project = Project(project_root)
    if iteration is None:
        it, store, _ = project.start_iteration()
    else:
        it = project.find(iteration)
        store = project.open(it.id)
        last = store.last()
        store.append("SessionAttached", {"from_seq": last.seq if last else 0},
                     cause=next(iter(store.read(types_=("SessionStarted",))), None))
    app = BeckApp(reactor_for(project, it.id, store, agents))
    try:
        app.run()
        return app.result
    finally:
        store.close()
