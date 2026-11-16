"""What the agents consume, projected from the events into .beck/beck.db, per iteration.

The projection reads three events: AgentTurnEnded (a turn: its tokens and its time), AgentTurnFailed (a turn
the CLI could not complete) and SignalRejected (a turn whose output the protocol refused; its tokens are in
the AgentTurnEnded before it). It writes four tables, always in one transaction:

* `usage_event`, the ledger: one row per projected event, with its id, seq, correlation and command. Every
  number of the two tables below is a sum of these rows, so every statistic leads back to the events that made
  it (`beck usage --events`, then `beck events --correlation`). It is the dashboard's outbox too: a
  dashboard wakes when a row lands after its cursor (a rowid; many dashboards read it, none marks it handled).
* `usage`, per iteration, agent, model and command;
* `usage_window`, per iteration, time window (`usage.window_minutes`, by the events' time) and agent;
* `usage_cursor`, the last seq projected per iteration, and the window it was projected with.

Nothing here is a fact: the tables can be dropped and projected again from the events (`beck usage
--rebuild`). Any process can catch up (beck while it runs, the dashboard, a replay): the transaction is
immediate, so two of them never project the same event twice.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from beck.events.store import Event, EventStore

KINDS = ("AgentTurnEnded", "AgentTurnFailed", "SignalRejected")
NUMBERS = ("turns", "failed", "rejected", "input", "output", "cache_read", "cache_creation", "ms")

_COUNTS = ", ".join(f"{n} INTEGER NOT NULL DEFAULT 0" for n in NUMBERS)
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS usage_event (
  rowid          INTEGER PRIMARY KEY AUTOINCREMENT,   -- the dashboards' cursor
  iteration      TEXT NOT NULL,
  event_id       TEXT NOT NULL UNIQUE,
  seq            INTEGER NOT NULL,
  correlation_id TEXT NOT NULL,
  command_id     TEXT,                                -- the CommandIssued of the turn
  kind           TEXT NOT NULL,                       -- the event's type
  at             TEXT NOT NULL,
  win            TEXT NOT NULL,                       -- the window's start
  agent          TEXT NOT NULL,
  model          TEXT NOT NULL,
  command        TEXT NOT NULL,
  unit           TEXT,
  {_COUNTS}
);
CREATE INDEX IF NOT EXISTS usage_event_iteration ON usage_event(iteration, seq);
CREATE TABLE IF NOT EXISTS usage (
  iteration TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL, command TEXT NOT NULL,
  {_COUNTS}, events INTEGER NOT NULL DEFAULT 0, first_seq INTEGER, last_seq INTEGER,
  PRIMARY KEY (iteration, agent, model, command)
);
CREATE TABLE IF NOT EXISTS usage_window (
  iteration TEXT NOT NULL, win TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL,
  {_COUNTS}, events INTEGER NOT NULL DEFAULT 0, first_seq INTEGER, last_seq INTEGER,
  PRIMARY KEY (iteration, win, agent, model)
);
CREATE TABLE IF NOT EXISTS usage_cursor (
  iteration TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, window_minutes INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Row:
    """A row of the ledger: what one event added."""

    event_id: str
    seq: int
    correlation_id: str
    command_id: str | None
    kind: str
    at: str
    win: str
    agent: str
    model: str
    command: str
    unit: str | None
    numbers: dict[str, int]


def window_of(at: str, minutes: int) -> str:
    """The start of the window an event's time falls in, as `2026-11-16T18:50`.

    >>> window_of("2026-11-16T18:57:03.123+00:00", 10)
    '2026-11-16T18:50'
    """
    t = datetime.fromisoformat(at)
    start = t - timedelta(minutes=t.minute % minutes, seconds=t.second, microseconds=t.microsecond)
    return start.strftime("%Y-%m-%dT%H:%M")


class Usage:
    def __init__(self, root: Path | str, window_minutes: int = 10):
        self.window = window_minutes
        path = Path(root) / ".beck" / "beck.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- the projection ---

    def catch_up(self, store: EventStore, iteration: str) -> int:
        """Projects the events after the cursor; how many rows it added to the ledger."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute("SELECT * FROM usage_cursor WHERE iteration = ?", (iteration,)).fetchone()
            if cursor is not None and cursor["window_minutes"] != self.window:   # windows of another size
                self._clear(iteration)
                cursor = None
            after = cursor["last_seq"] if cursor else 0
            added, last = 0, after
            for event in store.read(after=after):
                last = event.seq
                if event.type in KINDS:
                    self._add(iteration, self._row(store, iteration, event))
                    added += 1
            self._conn.execute("INSERT INTO usage_cursor VALUES (?, ?, ?) ON CONFLICT(iteration) DO UPDATE SET "
                               "last_seq = excluded.last_seq, window_minutes = excluded.window_minutes",
                               (iteration, last, self.window))
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return added

    def rebuild(self, store: EventStore, iteration: str) -> int:
        self._conn.execute("BEGIN IMMEDIATE")
        self._clear(iteration)
        self._conn.execute("COMMIT")
        return self.catch_up(store, iteration)

    def _clear(self, iteration: str) -> None:
        for table in ("usage_event", "usage", "usage_window", "usage_cursor"):
            self._conn.execute(f"DELETE FROM {table} WHERE iteration = ?", (iteration,))

    def _row(self, store: EventStore, iteration: str, event: Event) -> Row:
        p = event.payload
        command = store.get(event.causation_id) if event.causation_id else None
        name = (command.payload.get("command") if command and command.type == "CommandIssued" else None)
        name = name or p.get("command") or "-"
        unit = (command.payload.get("args") or {}).get("unit") if command and command.type == "CommandIssued" else None
        numbers: dict[str, int] = {n: 0 for n in NUMBERS}
        if event.type == "AgentTurnEnded":
            tokens = p.get("tokens") or {}
            numbers["turns"], numbers["ms"] = 1, int(p.get("duration_ms", 0))
            for k in ("input", "output", "cache_read", "cache_creation"):
                numbers[k] = int(tokens.get(k, 0))
            model = p.get("model") or "-"
        else:
            numbers["failed" if event.type == "AgentTurnFailed" else "rejected"] = 1
            model = self._model_of(iteration, p["agent"], event.causation_id)
        return Row(event.id, event.seq, event.correlation_id, event.causation_id, event.type, event.at,
                   window_of(event.at, self.window), p["agent"], model, name, unit, numbers)

    def _model_of(self, iteration: str, agent: str, command_id: str | None) -> str:
        """A failed or rejected turn's model: its own turn's, else the agent's last."""
        row = self._conn.execute(
            "SELECT model FROM usage_event WHERE iteration = ? AND agent = ? AND kind = 'AgentTurnEnded' "
            "ORDER BY command_id = ? DESC, seq DESC LIMIT 1", (iteration, agent, command_id)).fetchone()
        return row["model"] if row else "-"

    def _add(self, iteration: str, r: Row) -> None:
        values = [r.numbers[n] for n in NUMBERS]
        cols = ", ".join(NUMBERS)
        marks = ", ".join("?" * len(NUMBERS))
        self._conn.execute(
            f"INSERT INTO usage_event (iteration, event_id, seq, correlation_id, command_id, kind, at, win, agent, "
            f"model, command, unit, {cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {marks})",
            (iteration, r.event_id, r.seq, r.correlation_id, r.command_id, r.kind, r.at, r.win, r.agent, r.model,
             r.command, r.unit, *values))
        adds = ", ".join(f"{n} = {n} + excluded.{n}" for n in NUMBERS)
        tail = ("events = events + 1, first_seq = min(first_seq, excluded.first_seq), "
                "last_seq = max(last_seq, excluded.last_seq)")
        for table, keys, key_values in (("usage", "iteration, agent, model, command",
                                         (iteration, r.agent, r.model, r.command)),
                                        ("usage_window", "iteration, win, agent, model",
                                         (iteration, r.win, r.agent, r.model))):
            self._conn.execute(
                f"INSERT INTO {table} ({keys}, {cols}, events, first_seq, last_seq) "
                f"VALUES ({', '.join('?' * len(key_values))}, {marks}, 1, ?, ?) "
                f"ON CONFLICT({keys}) DO UPDATE SET {adds}, {tail}", (*key_values, *values, r.seq, r.seq))

    # --- the readings ---

    def by(self, iteration: str, key: str = "agent") -> list[dict[str, Any]]:
        """The totals grouped by agent, model, command (`usage`) or window (`usage_window`)."""
        table, cols = {"agent": ("usage", "agent, model"), "model": ("usage", "model"),
                       "command": ("usage", "agent, command"), "window": ("usage_window", "win")}[key]
        sums = ", ".join(f"sum({n}) AS {n}" for n in (*NUMBERS, "events"))
        rows = self._conn.execute(f"SELECT {cols}, {sums}, min(first_seq) AS first_seq, max(last_seq) AS last_seq "
                                  f"FROM {table} WHERE iteration = ? GROUP BY {cols} ORDER BY {cols}", (iteration,))
        return [dict(r) for r in rows]

    def windows(self, iteration: str) -> list[dict[str, Any]]:
        """Each window's numbers per agent, in time order."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM usage_window WHERE iteration = ? ORDER BY win, agent", (iteration,))]

    def total(self, iteration: str) -> dict[str, int]:
        sums = ", ".join(f"coalesce(sum({n}), 0) AS {n}" for n in (*NUMBERS, "events"))
        return dict(self._conn.execute(f"SELECT {sums} FROM usage WHERE iteration = ?", (iteration,)).fetchone())

    def events(self, iteration: str, after: int = 0, **where: str) -> Iterator[dict[str, Any]]:
        """The ledger's rows after a rowid, optionally of one agent, model, command, unit, kind or window."""
        allowed = {"agent", "model", "command", "unit", "kind", "win"}
        unknown = set(where) - allowed
        if unknown:
            raise ValueError(f"unknown filter {sorted(unknown)}")
        sql = "SELECT * FROM usage_event WHERE iteration = ? AND rowid > ?"
        args: list[object] = [iteration, after]
        for k, v in where.items():
            sql, args = sql + f" AND {k} = ?", [*args, v]
        for r in self._conn.execute(sql + " ORDER BY rowid", args):
            yield dict(r)

    def latest(self, iteration: str) -> int:
        """The last rowid of the iteration's ledger: a dashboard redraws when it moves."""
        row = self._conn.execute("SELECT coalesce(max(rowid), 0) FROM usage_event WHERE iteration = ?",
                                 (iteration,)).fetchone()
        return int(row[0])


def tokens(r: dict[str, Any]) -> int:
    """What a row consumed: the tokens in and out (the cache's are apart, they cost another price)."""
    return int(r["input"] + r["output"])


def projector(usage: Usage, store: EventStore, iteration: str) -> Any:
    """The Reactor's hook: catch up after each round of policies."""
    return lambda: usage.catch_up(store, iteration)
