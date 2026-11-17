"""The domain specs: one per domain of the architecture, `.beck/domains/<id>.yaml` (docs/dsl.md).

Domains emerge from the specs: kent splits a spec when two different concepts share a name (the
validator calls a duplicate name out as that smell), when a group of invariants does not depend on the
others' (a bounded context is a black box of invariants), or when a part would appear as it is in a
similar application (a generic domain, a library to be). Each spec says which of the three kinds it is:
`core` (what makes the application unlike the others), `supporting` (its own, but no advantage),
`generic` (reusable, or to buy).

Inside a spec the names are plain; in the graph an entity or an event is `<domain>/<Name>`, so two
domains may each have their own `Bookmark`. The ids of the requirements and the morphisms stay unique
across the specs: a reference to the specs (`spec: <id>`) names one without its domain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FOLDER = "domains"


def folder(beck_dir: Path) -> Path:
    return beck_dir / FOLDER


def load(beck_dir: Path) -> dict[str, Any]:
    """The specs by the name of their file (the domain's id); a spec that is not YAML holds its error."""
    specs: dict[str, Any] = {}
    for path in sorted(folder(beck_dir).glob("*.yaml")):
        try:
            specs[path.stem] = yaml.safe_load(path.read_text())
        except yaml.YAMLError as error:
            specs[path.stem] = {"__yaml_error__": str(error)}
    return specs


def qualify(domain: str, name: str) -> str:
    return f"{domain}/{name}"


def plain(name: str) -> str:
    """An entity's or an event's name without its domain: `tagging/Bookmark` → `Bookmark`."""
    return name.rsplit("/", 1)[-1]


def merge(specs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The valid specs as one document for the graph, the names of the entities and events qualified."""
    out: dict[str, Any] = {"entities": [], "events": [], "relations": [], "constraints": [], "morphisms": [],
                           "properties": [], "domains": {}}
    for domain, doc in sorted(specs.items()):
        def q(name: str, d: str = domain) -> str:
            return qualify(d, name)
        out["domains"][domain] = doc["subtype"]
        out.setdefault("system", doc["system"])
        out["entities"] += [{**e, "name": q(e["name"]), "domain": domain} for e in doc["entities"]]
        out["events"] += [{**e, "name": q(e["name"]), "domain": domain} for e in doc.get("events", [])]
        out["relations"] += [{**r, "from": q(r["from"]), "to": q(r["to"])} for r in doc.get("relations", [])]
        out["constraints"] += [{**c, "about": [q(n) for n in c["about"]]} for c in doc.get("constraints", [])]
        out["morphisms"] += [{**m, "uses": [q(n) for n in m["uses"]], "emits": [q(n) for n in m.get("emits", [])],
                              "domain": domain} for m in doc["morphisms"]]
        out["properties"] += [{**p, "about": [q(n) for n in p["about"]]} for p in doc.get("properties", [])]
    return out


def ids_of(doc: dict[str, Any]) -> set[str]:
    """The ids a spec defines: its morphisms and its requirements."""
    ids = {m["id"] for m in doc.get("morphisms", [])}
    ids |= {r["id"] for m in doc.get("morphisms", []) for r in m.get("post", []) + m.get("errors", [])}
    ids |= {i["id"] for e in doc.get("entities", []) for i in e.get("invariants", [])}
    ids |= {r["id"] for rel in doc.get("relations", []) for r in rel.get("rules", [])}
    ids |= {c["id"] for c in doc.get("constraints", [])} | {p["id"] for p in doc.get("properties", [])}
    return ids
