"""The event store of one iteration: an append-only SQLite file holding only events.

Every event has an id; `causation_id` is the event that directly caused it
(the one a policy reacted to), `correlation_id` the root of its chain (an
event with no cause is its own root). Messages, projections and log lines
carry the same ids, so whatever happened can be traced back to an event.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from beck.events import types

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  id             TEXT NOT NULL UNIQUE,
  type           TEXT NOT NULL,
  stream         TEXT NOT NULL,   -- session | component:<id> | cycle:<component>/<requirement> | ...
  actor          TEXT NOT NULL,   -- beck | user | <agent>
  causation_id   TEXT REFERENCES event(id),
  correlation_id TEXT NOT NULL,
  policy         TEXT,            -- the policy whose command made it
  sha            TEXT,            -- the repository's commit when the event happened
  tokens         TEXT,            -- JSON, for an agent's turn
  payload        TEXT NOT NULL,   -- JSON, checked against the type's schema
  at             TEXT NOT NULL    -- ISO 8601, UTC
);
CREATE INDEX IF NOT EXISTS event_stream ON event(stream, seq);
CREATE INDEX IF NOT EXISTS event_correlation ON event(correlation_id, seq);
CREATE INDEX IF NOT EXISTS event_causation ON event(causation_id, policy);
CREATE TRIGGER IF NOT EXISTS event_append_only_update BEFORE UPDATE ON event
  BEGIN SELECT RAISE(ABORT, 'the event store is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_append_only_delete BEFORE DELETE ON event
  BEGIN SELECT RAISE(ABORT, 'the event store is append-only'); END;
"""


@dataclass(frozen=True)
class Event:
    seq: int
    id: str
    type: str
    stream: str
    actor: str
    causation_id: str | None
    correlation_id: str
    policy: str | None
    sha: str | None
    tokens: dict[str, int] | None
    payload: dict[str, Any]
    at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(row["seq"], row["id"], row["type"], row["stream"], row["actor"], row["causation_id"],
                   row["correlation_id"], row["policy"], row["sha"], json.loads(row["tokens"]) if row["tokens"] else None,
                   json.loads(row["payload"]), row["at"])


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class EventStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 10000")  # readers (the dashboard, `beck events`) beside the engine
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def append(self, type_: str, payload: dict[str, Any], *, stream: str = "session", actor: str = "beck",
               cause: Event | None = None, sha: str | None = None, tokens: dict[str, int] | None = None,
               policy: str | None = None) -> Event:
        """Appends one event; `cause` is the event a policy reacted to, if any."""
        types.check(type_, payload)
        id_ = str(uuid.uuid4())
        correlation = cause.correlation_id if cause is not None else id_
        self._conn.execute(
            "INSERT INTO event (id, type, stream, actor, causation_id, correlation_id, policy, sha, tokens, "
            "payload, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id_, type_, stream, actor, cause.id if cause else None, correlation, policy, sha,
             json.dumps(tokens) if tokens is not None else None, json.dumps(payload), now()))
        return self.get(id_)

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Appends inside it land all together or not at all."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def caused_by(self, id_: str) -> list[Event]:
        """The events the event caused directly."""
        return [Event.from_row(r) for r in
                self._conn.execute("SELECT * FROM event WHERE causation_id = ? ORDER BY seq", (id_,))]

    def issued(self, cause_id: str, policy: str) -> bool:
        """Whether the policy already reacted to the event."""
        return self._conn.execute("SELECT 1 FROM event WHERE causation_id = ? AND policy = ?",
                                  (cause_id, policy)).fetchone() is not None

    def get(self, id_: str) -> Event:
        row = self._conn.execute("SELECT * FROM event WHERE id = ?", (id_,)).fetchone()
        if row is None:
            raise KeyError(id_)
        return Event.from_row(row)

    def read(self, *, stream: str | None = None, after: int = 0, types_: tuple[str, ...] = (),
             correlation_id: str | None = None) -> Iterator[Event]:
        """The events in order, optionally of one stream, after a seq, of some types or of one chain."""
        sql, args = "SELECT * FROM event WHERE seq > ?", list[object]([after])
        if stream is not None:
            sql, args = sql + " AND stream = ?", args + [stream]
        if types_:
            sql, args = sql + f" AND type IN ({','.join('?' * len(types_))})", args + list(types_)
        if correlation_id is not None:
            sql, args = sql + " AND correlation_id = ?", args + [correlation_id]
        for row in self._conn.execute(sql + " ORDER BY seq", args):
            yield Event.from_row(row)

    def last(self) -> Event | None:
        row = self._conn.execute("SELECT * FROM event ORDER BY seq DESC LIMIT 1").fetchone()
        return Event.from_row(row) if row else None

    def causes(self, id_: str) -> list[Event]:
        """The chain from the root down to the event: what caused it, step by step."""
        chain = [self.get(id_)]
        while chain[0].causation_id is not None:
            chain.insert(0, self.get(chain[0].causation_id))
        return chain

    def effects(self, id_: str) -> list[Event]:
        """Everything the event caused, directly or not, in order."""
        rows = self._conn.execute(
            "WITH RECURSIVE down(id) AS (SELECT id FROM event WHERE causation_id = ? "
            "UNION SELECT e.id FROM event e JOIN down d ON e.causation_id = d.id) "
            "SELECT e.* FROM event e JOIN down d ON e.id = d.id ORDER BY e.seq", (id_,))
        return [Event.from_row(r) for r in rows]


# --- messages and projections carry the event they come from ---------------


@dataclass(frozen=True)
class Message:
    """What a policy sends to an actor: a command, with the event that caused it."""

    command: str
    target: str
    payload: dict[str, Any]
    causation_id: str
    correlation_id: str

    @classmethod
    def caused_by(cls, event: Event, command: str, target: str, payload: dict[str, Any] | None = None) -> Message:
        return cls(command, target, payload or {}, event.id, event.correlation_id)


S = TypeVar("S")


@dataclass(frozen=True)
class Projection(Generic[S]):
    """A state folded from events, with the last event it folded."""

    state: S
    last_event_id: str | None
    last_seq: int


def fold(events: Iterator[Event] | list[Event], initial: S, step: Callable[[S, Event], S]) -> Projection[S]:
    state, last_id, last_seq = initial, None, 0
    for event in events:
        state, last_id, last_seq = step(state, event), event.id, event.seq
    return Projection(state, last_id, last_seq)
