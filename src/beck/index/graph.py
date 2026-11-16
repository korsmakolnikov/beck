"""From the two DSLs to the graph (docs/dsl.md, "From the DSLs to the graph").

Expects documents that `beck validate` accepted: it does not check them again.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from beck.index.store import INTEGRATION, add_edge, add_node


def load_domain(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    for e in doc["entities"]:
        add_node(conn, e["name"], "entity", subkind=e["kind"], text=e["description"], fields=e["fields"])
        for inv in e.get("invariants", []):
            add_node(conn, inv["id"], "requirement", subkind="invariant", owner=e["name"], text=inv["rule"])
            add_edge(conn, inv["id"], e["name"], "binds")
    for ev in doc.get("events", []):
        add_node(conn, ev["name"], "event", text=ev["description"], fields=ev.get("fields", []))
    for r in doc.get("relations", []):
        for rule in r.get("rules", []):
            add_node(conn, rule["id"], "requirement", subkind="rule", owner=r["from"], text=rule["rule"],
                     relation={"from": r["from"], "to": r["to"], "kind": r["kind"]})
            add_edge(conn, rule["id"], r["from"], "binds")
            add_edge(conn, rule["id"], r["to"], "binds")
    for c in doc.get("constraints", []):
        add_node(conn, c["id"], "requirement", subkind="constraint", text=c["rule"])
        for name in c["about"]:
            add_edge(conn, c["id"], name, "binds")
    for op in doc["morphisms"]:
        add_node(conn, op["id"], "morphism", text=op["description"], pre=op.get("pre", []),
                 emits=op.get("emits", []))
        for name in op["uses"]:
            add_edge(conn, op["id"], name, "uses")
        for post in op["post"]:
            add_node(conn, post["id"], "requirement", subkind="post", owner=op["id"], text=post["rule"])
            add_edge(conn, post["id"], op["id"], "binds")
        for err in op.get("errors", []):
            add_node(conn, err["id"], "requirement", subkind="error", owner=op["id"], text=err["outcome"],
                     when=err["when"])
            add_edge(conn, err["id"], op["id"], "binds")
    for p in doc.get("properties", []):   # an invariant: of its entity, or of the component over several
        about = p["about"]
        add_node(conn, p["id"], "requirement", subkind="property", owner=about[0] if len(about) == 1 else None,
                 text=p["holds"], property_kind=p["kind"], forall=p["forall"], morphisms=p.get("morphisms", []))
        for name in about:
            add_edge(conn, p["id"], name, "binds")
        for op in p.get("morphisms", []):
            add_edge(conn, p["id"], op, "binds")


def load_architecture(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    system = doc["system"]
    add_node(conn, system["id"], "container", subkind="system", text=system["name"],
             namespace=[system["physical"]])

    def element(e: dict[str, Any], parent: str, namespace: list[str]) -> None:
        external = e["kind"] == "library" and "package" in e
        here = [e["physical"]] if external else namespace + [e["physical"]]
        kind = e["kind"] if e["kind"] in ("component", "library") else "container"
        add_node(conn, e["id"], kind, subkind=e["kind"], text=e.get("description", e["name"]),
                 name=e["name"], namespace=here, boundary=e.get("boundary"),
                 supervised=e.get("supervised", False), package=e.get("package"), version=e.get("version"))
        add_edge(conn, parent, e["id"], "contains")
        for child in e.get("elements", []):
            element(child, e["id"], here)

    for e in doc["elements"]:
        element(e, system["id"], [system["physical"]])
    add_node(conn, INTEGRATION, "component", subkind="integration", text="The integration component.",
             namespace=[system["physical"]])

    def wire(e: dict[str, Any]) -> None:
        for dep in e.get("depends_on", []):
            add_edge(conn, e["id"], dep, "depends_on")
        for id_ in e.get("implements", []):
            implement(e["id"], id_)
        for child in e.get("elements", []):
            wire(child)

    def implement(component: str, id_: str) -> None:
        add_edge(conn, component, id_, "implements")
        # a morphism brings its post and its errors
        for row in conn.execute("SELECT id FROM node WHERE kind = 'requirement' AND owner = ? AND "
                                "subkind IN ('post', 'error')", (id_,)):
            add_edge(conn, component, row["id"], "implements")

    for e in doc["elements"]:
        wire(e)
    for id_ in doc.get("integration", {}).get("implements", []):
        implement(INTEGRATION, id_)
