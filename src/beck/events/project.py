"""The project database, .beck/beck.db: what does not derive from the events.

* the catalog of the iterations (one event store each);
* the outbox smart_audit writes and argyris reads: whether a record has been
  handled is state of its own, and it outlives the iteration it came from.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beck.events.store import Event, EventStore, now

# what beck derives or records, never versioned even when .beck/ is
GITIGNORE = "events/\nreports/\nworktrees/\nindex.db\nbeck.db\nbeck.db-*\nbeck.log\nagents.json\nargyris.json\n"

SCHEMA = """
CREATE TABLE IF NOT EXISTS iteration (
  id         TEXT PRIMARY KEY,   -- the event store's id: .beck/events/<id>.db
  name       TEXT,               -- the branch, once created
  started_at TEXT NOT NULL,
  ended_at   TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  id             TEXT PRIMARY KEY,
  iteration      TEXT NOT NULL REFERENCES iteration(id),
  event_id       TEXT NOT NULL,   -- the event the finding is about
  correlation_id TEXT NOT NULL,
  kind           TEXT NOT NULL,   -- the violation's kind
  payload        TEXT NOT NULL,   -- JSON
  created_at     TEXT NOT NULL,
  handled_at     TEXT,
  handled_by     TEXT,
  handled_how    TEXT             -- JSON: argyris's settlement (how, files, reason)
);
"""


@dataclass(frozen=True)
class Iteration:
    id: str
    name: str | None
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class OutboxRecord:
    id: str
    iteration: str
    event_id: str
    correlation_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str
    handled_at: str | None
    handled_how: dict[str, Any] | None = None


class Project:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()   # absolute: agents run in the units' worktrees, not in the root
        self.beck_dir = self.root / ".beck"
        self.beck_dir.mkdir(parents=True, exist_ok=True)
        ignore = self.beck_dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text(GITIGNORE)
        self._conn = sqlite3.connect(self.beck_dir / "beck.db", isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(outbox)")}
        if "handled_how" not in columns:   # a beck.db written before the settlements
            self._conn.execute("ALTER TABLE outbox ADD COLUMN handled_how TEXT")

    def close(self) -> None:
        self._conn.close()

    def store_path(self, iteration: str) -> Path:
        return self.beck_dir / "events" / f"{iteration}.db"

    # --- the catalog ---

    def start_iteration(self) -> tuple[Iteration, EventStore, Event]:
        """A new event store with a random id, its catalog row and its SessionStarted."""
        id_ = str(uuid.uuid4())
        self._conn.execute("INSERT INTO iteration (id, started_at) VALUES (?, ?)", (id_, now()))
        store = EventStore(self.store_path(id_))
        started = store.append("SessionStarted", {"project": str(self.root.resolve())})
        return self.iteration(id_), store, started

    def open(self, iteration: str) -> EventStore:
        self.iteration(iteration)
        return EventStore(self.store_path(iteration))

    def iteration(self, id_: str) -> Iteration:
        row = self._conn.execute("SELECT * FROM iteration WHERE id = ?", (id_,)).fetchone()
        if row is None:
            raise KeyError(f"no iteration {id_!r}")
        return Iteration(row["id"], row["name"], row["started_at"], row["ended_at"])

    def find(self, prefix: str) -> Iteration:
        """The iteration whose id starts with prefix, as `git` takes a short sha."""
        rows = self._conn.execute("SELECT id FROM iteration WHERE id LIKE ? || '%'", (prefix,)).fetchall()
        if len(rows) != 1:
            raise KeyError(f"{len(rows)} iterations match {prefix!r}")
        return self.iteration(rows[0]["id"])

    def name_iteration(self, id_: str, name: str) -> None:
        self._conn.execute("UPDATE iteration SET name = ? WHERE id = ?", (name, id_))

    def end_iteration(self, id_: str) -> None:
        self._conn.execute("UPDATE iteration SET ended_at = ? WHERE id = ?", (now(), id_))

    def iterations(self) -> list[Iteration]:
        return [Iteration(r["id"], r["name"], r["started_at"], r["ended_at"])
                for r in self._conn.execute("SELECT * FROM iteration ORDER BY started_at")]

    # --- the outbox ---

    def post(self, iteration: str, event: Event, kind: str, payload: dict[str, Any]) -> OutboxRecord:
        id_ = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO outbox (id, iteration, event_id, correlation_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id_, iteration, event.id, event.correlation_id, kind, json.dumps(payload), now()))
        return self._record(id_)

    def pending(self) -> list[OutboxRecord]:
        return [self._record(r["id"]) for r in
                self._conn.execute("SELECT id FROM outbox WHERE handled_at IS NULL ORDER BY created_at")]

    def handle(self, id_: str, by: str, how: dict[str, Any] | None = None) -> None:
        self._record(id_)
        self._conn.execute("UPDATE outbox SET handled_at = ?, handled_by = ?, handled_how = ? WHERE id = ?",
                           (now(), by, json.dumps(how) if how is not None else None, id_))

    def outbox(self, id_: str) -> OutboxRecord:
        return self._record(id_)

    def _record(self, id_: str) -> OutboxRecord:
        r = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (id_,)).fetchone()
        if r is None:
            raise KeyError(f"no outbox record {id_!r}")
        return OutboxRecord(r["id"], r["iteration"], r["event_id"], r["correlation_id"], r["kind"],
                            json.loads(r["payload"]), r["created_at"], r["handled_at"],
                            json.loads(r["handled_how"]) if r["handled_how"] else None)
