"""beck dashboard --attach: the consumption of the session running in this folder, live (Textual).

    ┌ beck · <iteration> · attached ────────────────────────────────────────────────────┐
    │ totals: turns · failed · rejected · tokens in/out · cache · time                   │
    │ per agent and model                       │ tokens per window (usage.window_minutes) │
    ├ the log, as it is written ─────────────────────────────────────────────────────────┤
    └────────────────────────────────────────────────────────────────────────────────────┘

The dashboard projects nothing: it attaches to the session's socket (the folder names the session), and
the handshake names the read models' database and the log. Whenever the session signals that the read
models changed, the dashboard reads them again (beck/usage.py) and redraws; the log pane follows the log's
file on its own. When the session ends, the dashboard says so and keeps the last picture.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Log, Static

from beck import config as beck_config
from beck import socket
from beck.usage import Usage, tokens

COLOURS = ("cyan", "magenta", "green", "yellow", "blue", "red", "bright_black")
LOG_LINES = 500


def _seconds(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _per_turn(r: dict[str, Any]) -> str:
    return f"{tokens(r) // r['turns']:,}" if r["turns"] else "-"


def colours(usage: Usage, iteration: str) -> dict[str, str]:
    agents = sorted({r["agent"] for r in usage.by(iteration, "agent")})
    return {a: COLOURS[i % len(COLOURS)] for i, a in enumerate(agents)}


def totals(usage: Usage, iteration: str) -> RenderableType:
    t = usage.total(iteration)
    return Text(f"turns {t['turns']}   failed {t['failed']}   rejected {t['rejected']}   "
                f"tokens {t['input']:,} in / {t['output']:,} out   cache {t['cache_read']:,} read / "
                f"{t['cache_creation']:,} written   time {_seconds(t['ms'])}   per turn {_per_turn(t)}")


def agents(usage: Usage, iteration: str) -> RenderableType:
    """Per agent and model: the turns, how they ended, the tokens and the time, and a bar of the tokens."""
    colour = colours(usage, iteration)
    table = Table(expand=True, box=None, header_style="bold", title="per agent", title_justify="left")
    for col, right in (("agent", False), ("model", False), ("turns", True), ("failed", True), ("rejected", True),
                       ("in", True), ("out", True), ("per turn", True), ("time", True), ("s/turn", True)):
        table.add_column(col, justify="right" if right else "left")
    rows = usage.by(iteration, "agent")
    if not rows:
        return Text("no turn yet", style="dim")
    most = max(tokens(r) for r in rows) or 1
    for r in rows:
        table.add_row(Text(r["agent"], style=colour[r["agent"]]), r["model"], str(r["turns"]), str(r["failed"]),
                      str(r["rejected"]), f"{r['input']:,}", f"{r['output']:,}", _per_turn(r), _seconds(r["ms"]),
                      f"{r['ms'] // 1000 // r['turns']}" if r["turns"] else "-")
    bars = [Text(f"{r['agent']:<10} ").append("█" * max(1, round(tokens(r) / most * 40)), style=colour[r["agent"]])
            .append(f" {tokens(r):,}") for r in rows]
    return Group(table, Text(), *bars)


def windows(usage: Usage, iteration: str, width: int = 60) -> RenderableType:
    """The tokens of each window of time, a bar each, a colour per agent."""
    colour = colours(usage, iteration)
    by_window: dict[str, list[dict[str, Any]]] = {}
    for r in usage.windows(iteration):
        by_window.setdefault(r["win"], []).append(r)
    if not by_window:
        return Text("no turn yet", style="dim")
    most = max(sum(tokens(r) for r in rs) for rs in by_window.values()) or 1
    lines = [Text(f"tokens per {usage.window} minutes", style="bold")]
    for win, rs in list(by_window.items())[-24:]:
        line = Text(f"{win[11:16]} ")
        for r in rs:
            line.append("█" * round(tokens(r) / most * width), style=colour.get(r["agent"], "white"))
        line.append(f" {sum(tokens(r) for r in rs):,}  {sum(r['turns'] for r in rs)} turns")
        lines.append(line)
    legend = Text()
    for a, c in colour.items():
        legend.append(f"■ {a}  ", style=c)
    return Group(*lines, legend)


class Dashboard(App[int]):
    CSS = """
    #totals { height: 1; background: $boost; padding: 0 1; }
    #charts { height: 2fr; }
    #agents { width: 1fr; border: round $primary; padding: 0 1; }
    #windows { width: 1fr; border: round $secondary; padding: 0 1; }
    #log { height: 1fr; border: round $accent; }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit (the session goes on)")]

    def __init__(self, project: Path):
        super().__init__()
        self.project = project
        self.usage: Usage | None = None
        self.iteration = ""
        self.log_path: Path | None = None
        self.redraws = 0
        self.ended = False

    def compose(self) -> ComposeResult:
        yield Static("attaching…", id="totals")
        with Horizontal(id="charts"):
            yield Static(id="agents")
            yield Static(id="windows")
        yield Log(id="log", max_lines=LOG_LINES)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self.listen(), exclusive=False)

    async def listen(self) -> None:
        """The handshake, then a redraw at every signal of the session."""
        try:
            reader, writer = await asyncio.open_unix_connection(str(socket.path(self.project)))
        except (FileNotFoundError, ConnectionRefusedError):
            self.query_one("#totals", Static).update(f"no beck session runs in {self.project}")
            return
        writer.write(b'{"hello": "dashboard"}\n')
        await writer.drain()
        hello = json.loads(await reader.readline())
        self.iteration, self.log_path = hello["iteration"], Path(hello["log"])
        try:
            minutes = beck_config.load(self.project)["usage"]["window_minutes"]
        except beck_config.ConfigError:
            minutes = beck_config.DEFAULTS["usage"]["window_minutes"]
        self.usage = Usage(Path(hello["db"]).parent.parent, minutes)
        self.title = f"beck · {self.iteration[:8]} · attached"
        self.run_worker(self.follow(), exclusive=False)
        self.redraw()
        while line := await reader.readline():
            if json.loads(line).get("signal") == "read":
                self.redraw()
        self.ended = True
        self.title = f"beck · {self.iteration[:8]} · the session ended"

    def redraw(self) -> None:
        assert self.usage is not None
        self.redraws += 1
        self.query_one("#totals", Static).update(totals(self.usage, self.iteration))
        self.query_one("#agents", Static).update(agents(self.usage, self.iteration))
        width = max(20, self.query_one("#windows", Static).size.width - 30)
        self.query_one("#windows", Static).update(windows(self.usage, self.iteration, width))

    async def follow(self) -> None:
        """The log's lines as they are written: the file followed, no signal needed."""
        assert self.log_path is not None
        pane = self.query_one("#log", Log)
        while not self.log_path.exists():
            await asyncio.sleep(0.3)
        with self.log_path.open() as f:
            pane.write_lines([line.rstrip("\n") for line in f.readlines()[-LOG_LINES:]])
            while True:
                line = f.readline()
                if line:
                    pane.write_line(line.rstrip("\n"))
                else:
                    await asyncio.sleep(0.3)


def run(project: Path) -> int:
    app = Dashboard(project.resolve())
    app.run()
    return app.return_code or 0
