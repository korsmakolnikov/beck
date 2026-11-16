"""The project database, .beck/beck.db: what does not derive from the events.

* the catalog of the iterations (one event store each);
* the read models (beck/usage.py).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from beck.events.store import Event, EventStore, now

# what beck derives or records, never versioned even when .beck/ is
GITIGNORE = "events/\nreports/\nworktrees/\nindex.db\nbeck.db\nbeck.db-*\nbeck.log\nagents.json\nbeck.sock\n"

SCHEMA = """
CREATE TABLE IF NOT EXISTS iteration (
  id         TEXT PRIMARY KEY,   -- the event store's id: .beck/events/<id>.db
  name       TEXT,               -- the branch, once created
  started_at TEXT NOT NULL,
  ended_at   TEXT
);
"""


@dataclass(frozen=True)
class Iteration:
    id: str
    name: str | None
    started_at: str
    ended_at: str | None


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

