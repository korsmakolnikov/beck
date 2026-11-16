"""From the graph to the sessions (docs/dsl.md, "From the graph to the sessions").

component = a component (+ the integration component); unit = an owner with its requirements,
one unclebob session; cycle = one requirement.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from beck.index.store import INTEGRATION, data

# inside a unit, from the particular to the general
_REQUIREMENT_ORDER = {"invariant": 0, "rule": 1, "post": 2, "error": 3, "property": 4, "constraint": 5}


@dataclass(frozen=True)
class Unit:
    id: str            # model:<Name> | morphism:<id> | model
    owner: str | None  # the entity or the morphism; None for the component unit
    requirements: tuple[str, ...]


def components(conn: sqlite3.Connection) -> list[str]:
    """The components in landing order (dependencies first); the integration component last, when it has work."""
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
    """The pairs of components that must not run in parallel: they share a file."""
    pairs: set[frozenset[str]] = set()
    for components in shared_files(conn).values():
        pairs |= {frozenset((a, b)) for i, a in enumerate(components) for b in components[i + 1:]}
    return pairs


def _implemented(conn: sqlite3.Connection, component: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT n.* FROM edge e JOIN node n ON n.id = e.dst "
        "WHERE e.src = ? AND e.kind = 'implements' ORDER BY n.rowid", (component,)).fetchall()


def _sorted(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    return tuple(r["id"] for r in sorted(rows, key=lambda r: _REQUIREMENT_ORDER[r["subkind"]]))


def units(conn: sqlite3.Connection, component: str) -> list[Unit]:
    implemented = _implemented(conn, component)
    requirements = [r for r in implemented if r["kind"] == "requirement"]
    morphisms = [r["id"] for r in implemented if r["kind"] == "morphism"]
    out: list[Unit] = []
    entities = [r["id"] for r in conn.execute("SELECT id FROM node WHERE kind = 'entity' ORDER BY rowid")]
    for entity in entities:
        mine = [c for c in requirements if c["owner"] == entity]
        if mine:
            out.append(Unit(f"model:{entity}", entity, _sorted(mine)))
    for op in morphisms:
        mine = [c for c in requirements if c["owner"] == op]
        if mine:
            out.append(Unit(f"morphism:{op}", op, _sorted(mine)))
    own = [c for c in requirements if c["owner"] is None]   # the component's own: spanning several entities
    if own:
        out.append(Unit("component", None, _sorted(own)))
    return out


def unit(conn: sqlite3.Connection, component: str, unit_id: str) -> Unit:
    for g in units(conn, component):
        if g.id == unit_id:
            return g
    raise KeyError(f"component {component!r} has no unit {unit_id!r}")


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


def context(conn: sqlite3.Connection, component: str, unit_id: str) -> dict[str, Any]:
    """Everything an unclebob session gets for one unit, and nothing else."""
    g = unit(conn, component, unit_id)
    component_row = _node(conn, component)
    requirements = []
    entity_names: list[str] = []
    for cid in g.requirements:
        row = _node(conn, cid)
        requirements.append({"id": cid, "kind": row["subkind"], "text": row["text"], **data(row),
                        "symbols": _symbols(conn, cid), "tests": _tests(conn, cid)})
        entity_names += [r["dst"] for r in conn.execute(
            "SELECT e.dst FROM edge e JOIN node n ON n.id = e.dst "
            "WHERE e.src = ? AND e.kind = 'binds' AND n.kind = 'entity'", (cid,))]

    morphism = None
    if g.id.startswith("morphism:") and g.owner is not None:
        op = _node(conn, g.owner)
        entity_names += [r["dst"] for r in conn.execute(
            "SELECT dst FROM edge WHERE src = ? AND kind = 'uses'", (op["id"],))]
        info = data(op)
        morphism = {"id": op["id"], "description": op["text"], "pre": info["pre"],
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
                          "WHERE e.src = ? AND e.kind = 'depends_on' ORDER BY n.rowid", (component,)):
        info = data(r)
        ops = [{"id": o["id"], "description": o["text"], "symbols": _symbols(conn, o["id"])}
               for o in _implemented(conn, r["id"]) if o["kind"] == "morphism"]
        dependencies.append({"id": r["id"], "kind": r["kind"], "namespace": info["namespace"],
                             "boundary": info.get("boundary"), "morphisms": ops})

    component_info = data(component_row)
    sources = sorted({s["file"] for c in [morphism or {}] + requirements for s in c.get("symbols", [])})
    tests = sorted({t["file"] for c in requirements for t in c["tests"]})
    return {
        "component": {"id": component, "namespace": component_info["namespace"], "boundary": component_info.get("boundary")},
        "unit": g.id,
        "morphism": morphism,
        "requirements": requirements,
        "entities": entities,
        "dependencies": dependencies,
        "files": {"source": sources, "tests": tests},
    }


def _event(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = _node(conn, name)
    return {"name": name, "description": row["text"], "fields": data(row)["fields"]}
