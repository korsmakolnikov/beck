"""beck dashboard: the consumption of an iteration, live, from the usage projection (beck/usage.py).

    ┌ beck · <iteration> <branch> · last: <event> ──────────────────────────────────────┐
    │ turns · failed · rejected · tokens in/out · cache · time · tokens per turn         │
    │ per agent and model: the same numbers                                              │
    │ tokens per window (usage.window_minutes), a bar per window, a colour per agent     │
    │ the ledger's last rows: time, event, agent, command, component, tokens, s, event id     │
    └────────────────────────────────────────────────────────────────────────────────────┘

It wakes on the ledger, the projection's outbox: every tick it projects what beck appended (so it works on
an iteration beck no longer runs, or never projected) and redraws when a row lands after its cursor. Every
row of the feed names its event: `beck events <iteration> --correlation <id>` shows what made it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from beck import config as beck_config
from beck.events.project import Project
from beck.events.store import EventStore
from beck.usage import Usage, tokens

COLOURS = ("cyan", "magenta", "green", "yellow", "blue", "red", "bright_black")
FEED = 10


def _seconds(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _per_turn(r: dict[str, Any]) -> str:
    return f"{tokens(r) // r['turns']:,}" if r["turns"] else "-"


def render(usage: Usage, store: EventStore, iteration: str, name: str | None, width: int = 100) -> RenderableType:
    last = store.last()
    title = f"beck · {iteration[:8]} {name or ''} · last: {f'{last.type} {last.at[11:19]}' if last else 'none'}"
    total = usage.total(iteration)
    head = Text(f"turns {total['turns']}   failed {total['failed']}   rejected {total['rejected']}   "
                f"tokens {total['input']:,} in / {total['output']:,} out   "
                f"cache {total['cache_read']:,} read / {total['cache_creation']:,} written   "
                f"time {_seconds(total['ms'])}   per turn {_per_turn(total)}")
    agents = usage.by(iteration, "agent")
    colour = {a: COLOURS[i % len(COLOURS)] for i, a in enumerate(sorted({r["agent"] for r in agents}))}
    table = Table(expand=True, box=None, header_style="bold")
    for col, right in (("agent", False), ("model", False), ("turns", True), ("failed", True), ("rejected", True),
                       ("in", True), ("out", True), ("cache read", True), ("per turn", True), ("time", True),
                       ("s/turn", True)):
        table.add_column(col, justify="right" if right else "left")
    for r in agents:
        table.add_row(Text(r["agent"], style=colour[r["agent"]]), r["model"], str(r["turns"]), str(r["failed"]),
                      str(r["rejected"]), f"{r['input']:,}", f"{r['output']:,}", f"{r['cache_read']:,}",
                      _per_turn(r), _seconds(r["ms"]), f"{r['ms'] // 1000 // r['turns']}" if r["turns"] else "-")
    return Panel(Group(head, Text(), table, Text(), _chart(usage, iteration, colour, width), Text(),
                       _feed(usage, iteration, colour)), title=title, title_align="left")


def _chart(usage: Usage, iteration: str, colour: dict[str, str], width: int) -> RenderableType:
    rows: dict[str, list[dict[str, Any]]] = {}
    for r in usage.windows(iteration):
        rows.setdefault(r["win"], []).append(r)
    if not rows:
        return Text("no turn yet", style="dim")
    most = max(sum(tokens(r) for r in rs) for rs in rows.values()) or 1
    bar_width = max(10, width - 32)
    lines = [Text(f"tokens per {usage.window} minutes", style="bold")]
    for win, rs in list(rows.items())[-24:]:     # the last four hours of ten minutes
        line = Text(f"{win[11:16]} ")
        for r in rs:
            line.append("█" * round(tokens(r) / most * bar_width), style=colour.get(r["agent"], "white"))
        line.append(f" {sum(tokens(r) for r in rs):,}  {sum(r['turns'] for r in rs)} turns")
        lines.append(line)
    legend = Text()
    for a, c in colour.items():
        legend.append(f"■ {a}  ", style=c)
    return Group(*lines, legend)


def _feed(usage: Usage, iteration: str, colour: dict[str, str]) -> RenderableType:
    rows = list(usage.events(iteration))[-FEED:]
    table = Table(expand=True, box=None, header_style="bold", title_justify="left",
                  title=f"the last turns (the ledger); what made one: beck trace {iteration[:8]} <event id>")
    for col in ("time", "event", "agent", "command", "component", "tokens", "s", "event id"):
        table.add_column(col)
    for r in reversed(rows):
        kind = {"AgentTurnEnded": "ended", "AgentTurnFailed": "failed", "SignalRejected": "rejected"}[r["kind"]]
        table.add_row(r["at"][11:19], Text(kind, style="red" if kind != "ended" else ""),
                      Text(r["agent"], style=colour.get(r["agent"], "")), r["command"], r["component"] or "-",
                      f"{tokens(r):,}" if r["turns"] else "-", str(r["ms"] // 1000) if r["turns"] else "-",
                      r["event_id"][:8])
    return table


def _latest(project: Project) -> str:
    iterations = project.iterations()
    if not iterations:
        raise KeyError("no iteration in this project")
    return iterations[-1].id


def run(root: Path, iteration: str | None, once: bool = False, every: float = 1.0) -> int:
    project = Project(root)
    it = project.find(iteration) if iteration else project.iteration(_latest(project))
    try:
        minutes = beck_config.load(project.root)["usage"]["window_minutes"]
    except beck_config.ConfigError:
        minutes = beck_config.DEFAULTS["usage"]["window_minutes"]
    usage, store, console = Usage(project.root, minutes), project.open(it.id), Console()
    usage.catch_up(store, it.id)
    if once:
        console.print(render(usage, store, it.id, it.name, console.width))
        return 0
    cursor = usage.latest(it.id)
    with Live(render(usage, store, it.id, it.name, console.width), console=console, screen=False,
              auto_refresh=False) as live:
        try:
            while True:
                time.sleep(every)
                usage.catch_up(store, it.id)
                latest = usage.latest(it.id)
                if latest != cursor:   # a row landed in the outbox: wake
                    cursor = latest
                    live.update(render(usage, store, it.id, it.name, console.width), refresh=True)
        except KeyboardInterrupt:
            return 0
