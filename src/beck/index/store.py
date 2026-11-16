"""The index's SQLite store: nodes, edges and the files they were read from."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE node (
  id      TEXT PRIMARY KEY,
  kind    TEXT NOT NULL,   -- component | library | container | entity | event | operation | clause | symbol | test
  subkind TEXT,            -- element kind; entity | value_object; invariant | rule | constraint | post | error | property
  owner   TEXT,            -- clause: the node whose group holds it (entity, operation) or NULL (model group)
  text    TEXT,            -- the clause's sentence, the description, the symbol's signature
  data    TEXT NOT NULL DEFAULT '{}'  -- JSON: fields, physical name, file and lines, ...
);
CREATE TABLE edge (
  src  TEXT NOT NULL REFERENCES node(id),
  dst  TEXT NOT NULL REFERENCES node(id),
  kind TEXT NOT NULL,      -- contains | depends_on | implements | uses | binds | anchored_to | tests
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX edge_dst ON edge(dst, kind);
CREATE TABLE file (
  path TEXT PRIMARY KEY,
  hash TEXT NOT NULL       -- sha256 of the content when indexed
);
"""

INTEGRATION = "@integration"  # the integration unit; '@' keeps it out of the DSL's ids


def create(path: Path | str) -> sqlite3.Connection:
    """A fresh index at path (the old one, if any, is replaced)."""
    path = Path(path)
    if path != Path(":memory:") and path.exists():
        path.unlink()
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_node(conn: sqlite3.Connection, id_: str, kind: str, *, subkind: str | None = None,
             owner: str | None = None, text: str | None = None, **data: Any) -> None:
    conn.execute(
        "INSERT INTO node (id, kind, subkind, owner, text, data) VALUES (?, ?, ?, ?, ?, ?)",
        (id_, kind, subkind, owner, text, json.dumps(data)),
    )


def add_edge(conn: sqlite3.Connection, src: str, dst: str, kind: str) -> None:
    conn.execute("INSERT OR IGNORE INTO edge (src, dst, kind) VALUES (?, ?, ?)", (src, dst, kind))


def data(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["data"])
