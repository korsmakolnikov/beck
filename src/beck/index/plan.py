"""From the graph to the sessions (docs/dsl.md, "From the graph to the sessions").

unit = a component (+ the integration unit); group = an owner with its clauses,
one unclebob session; cycle = one clause.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from beck.index.store import INTEGRATION, data

# inside a group, from the particular to the general
_CLAUSE_ORDER = {"invariant": 0, "rule": 1, "post": 2, "error": 3, "property": 4, "constraint": 5}


@dataclass(frozen=True)
class Group:
    id: str            # entity:<Name> | operation:<id> | model
    owner: str | None  # the entity or the operation; None for the model group
    clauses: tuple[str, ...]


def units(conn: sqlite3.Connection) -> list[str]:
    """The components in landing order (dependencies first); the integration unit last, when it has work."""
    components = [r["id"] for r in conn.execute(
        "SELECT id FROM node WHERE kind = 'component' AND id != ? ORDER BY rowid", (INTEGRATION,))]
    deps = {c: [r["dst"] for r in conn.execute(
        "SELECT e.dst FROM edge e JOIN node n ON n.id = e.dst "
        "WHERE e.src = ? AND e.kind = 'depends_on' AND n.kind = 'component' ORDER BY n.rowid", (c,))]
        for c in components}
    ordered: list[str] = []

    def visit(c: str) -> None:  # the validator has already refused cycles
        if c in ordered:
            return
        for d in deps[c]:
            visit(d)
        ordered.append(c)

    for c in components:
        visit(c)
    if _implemented(conn, INTEGRATION):
        ordered.append(INTEGRATION)
    return ordered


def shared_files(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """The files holding symbols of several components, with those components."""
    rows = conn.execute(
        "SELECT DISTINCT json_extract(s.data, '$.file') AS file, r.src AS component "
        "FROM node s JOIN edge a ON a.dst = s.id AND a.kind = 'anchored_to' "
        "JOIN edge r ON r.dst = a.src AND r.kind = 'implements' "
        "WHERE s.kind = 'symbol' ORDER BY file, component").fetchall()
    owners: dict[str, list[str]] = {}
    for row in rows:
        owners.setdefault(row["file"], []).append(row["component"])
    return {f: cs for f, cs in owners.items() if len(cs) > 1}


def exclusive(conn: sqlite3.Connection) -> set[frozenset[str]]:
    """The pairs of units that must not run in parallel: they share a file."""
    pairs: set[frozenset[str]] = set()
    for components in shared_files(conn).values():
        pairs |= {frozenset((a, b)) for i, a in enumerate(components) for b in components[i + 1:]}
    return pairs


def _implemented(conn: sqlite3.Connection, unit: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT n.* FROM edge e JOIN node n ON n.id = e.dst "
        "WHERE e.src = ? AND e.kind = 'implements' ORDER BY n.rowid", (unit,)).fetchall()


def _sorted(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    return tuple(r["id"] for r in sorted(rows, key=lambda r: _CLAUSE_ORDER[r["subkind"]]))


def groups(conn: sqlite3.Connection, unit: str) -> list[Group]:
    implemented = _implemented(conn, unit)
    clauses = [r for r in implemented if r["kind"] == "clause"]
    operations = [r["id"] for r in implemented if r["kind"] == "operation"]
    out: list[Group] = []
    entities = [r["id"] for r in conn.execute("SELECT id FROM node WHERE kind = 'entity' ORDER BY rowid")]
    for entity in entities:
        mine = [c for c in clauses if c["owner"] == entity]
        if mine:
            out.append(Group(f"entity:{entity}", entity, _sorted(mine)))
    for op in operations:
        mine = [c for c in clauses if c["owner"] == op]
        if mine:
            out.append(Group(f"operation:{op}", op, _sorted(mine)))
    model = [c for c in clauses if c["owner"] is None]
    if model:
        out.append(Group("model", None, _sorted(model)))
    return out


def group(conn: sqlite3.Connection, unit: str, group_id: str) -> Group:
    for g in groups(conn, unit):
        if g.id == group_id:
            return g
    raise KeyError(f"unit {unit!r} has no group {group_id!r}")


def _node(conn: sqlite3.Connection, id_: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM node WHERE id = ?", (id_,)).fetchone()


def _symbols(conn: sqlite3.Connection, id_: str) -> list[dict[str, Any]]:
    return [{"signature": r["text"], **data(r)} for r in conn.execute(
        "SELECT n.* FROM edge e JOIN node n ON n.id = e.dst "
        "WHERE e.src = ? AND e.kind = 'anchored_to' ORDER BY n.id", (id_,))]


def _tests(conn: sqlite3.Connection, id_: str) -> list[dict[str, Any]]:
    return [{"id": r["id"], **data(r)} for r in conn.execute(
        "SELECT n.* FROM edge e JOIN node n ON n.id = e.src "
        "WHERE e.dst = ? AND e.kind = 'tests' ORDER BY n.id", (id_,))]


def context(conn: sqlite3.Connection, unit: str, group_id: str) -> dict[str, Any]:
    """Everything an unclebob session gets for one group, and nothing else."""
    g = group(conn, unit, group_id)
    unit_row = _node(conn, unit)
    clauses = []
    entity_names: list[str] = []
    for cid in g.clauses:
        row = _node(conn, cid)
        clauses.append({"id": cid, "kind": row["subkind"], "text": row["text"], **data(row),
                        "symbols": _symbols(conn, cid), "tests": _tests(conn, cid)})
        entity_names += [r["dst"] for r in conn.execute(
            "SELECT e.dst FROM edge e JOIN node n ON n.id = e.dst "
            "WHERE e.src = ? AND e.kind = 'binds' AND n.kind = 'entity'", (cid,))]

    operation = None
    if g.id.startswith("operation:") and g.owner is not None:
        op = _node(conn, g.owner)
        entity_names += [r["dst"] for r in conn.execute(
            "SELECT dst FROM edge WHERE src = ? AND kind = 'uses'", (op["id"],))]
        info = data(op)
        operation = {"id": op["id"], "description": op["text"], "pre": info["pre"],
                     "emits": [_event(conn, name) for name in info["emits"]],
                     "symbols": _symbols(conn, op["id"])}
    elif g.owner is not None:
        entity_names.append(g.owner)

    order = {r["id"]: r["rowid"] for r in conn.execute("SELECT rowid, id FROM node WHERE kind = 'entity'")}
    entities = []
    for name in sorted(set(entity_names), key=order.__getitem__):
        row = _node(conn, name)
        entities.append({"name": name, "kind": row["subkind"], "description": row["text"],
                         "fields": data(row)["fields"]})

    dependencies = []
    for r in conn.execute("SELECT n.* FROM edge e JOIN node n ON n.id = e.dst "
                          "WHERE e.src = ? AND e.kind = 'depends_on' ORDER BY n.rowid", (unit,)):
        info = data(r)
        ops = [{"id": o["id"], "description": o["text"], "symbols": _symbols(conn, o["id"])}
               for o in _implemented(conn, r["id"]) if o["kind"] == "operation"]
        dependencies.append({"id": r["id"], "kind": r["kind"], "namespace": info["namespace"],
                             "boundary": info.get("boundary"), "operations": ops})

    unit_info = data(unit_row)
    sources = sorted({s["file"] for c in [operation or {}] + clauses for s in c.get("symbols", [])})
    tests = sorted({t["file"] for c in clauses for t in c["tests"]})
    return {
        "unit": {"id": unit, "namespace": unit_info["namespace"], "boundary": unit_info.get("boundary")},
        "group": g.id,
        "operation": operation,
        "clauses": clauses,
        "entities": entities,
        "dependencies": dependencies,
        "files": {"source": sources, "tests": tests},
    }


def _event(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = _node(conn, name)
    return {"name": name, "description": row["text"], "fields": data(row)["fields"]}
