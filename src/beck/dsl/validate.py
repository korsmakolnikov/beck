"""Validation of the two DSLs: the schema first, then the semantic checks.

docs/dsl.md, "Validation", lists the checks. The semantic checks run only on
a document that matches its schema, since they rely on its shape.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import Any, Iterator

import jsonschema
import yaml

SCHEMAS = files("beck.dsl") / "schemas"


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or '<root>'}: {self.message}"


@cache
def _validator(name: str) -> jsonschema.Draft202012Validator:
    schema = yaml.safe_load((SCHEMAS / f"{name}.schema.yaml").read_text())
    return jsonschema.Draft202012Validator(schema)


def _path(parts: Any) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def _schema_problems(name: str, doc: Any) -> list[Problem]:
    errors = sorted(_validator(name).iter_errors(doc), key=lambda e: list(map(str, e.path)))
    return [Problem(_path(e.path), e.message) for e in errors]


def _duplicates(where: list[tuple[str, str]], what: str) -> Iterator[Problem]:
    counts = Counter(value for value, _ in where)
    for value, path in where:
        if counts[value] > 1:
            yield Problem(path, f"{what} {value!r} is not unique")


# --- domain -----------------------------------------------------------------


@dataclass
class DomainIds:
    """The ids of a valid domain, as the architecture needs them."""

    all: dict[str, str] = field(default_factory=dict)  # id -> path
    operations: set[str] = field(default_factory=set)
    inherited: set[str] = field(default_factory=set)  # come with their operation
    explicit: set[str] = field(default_factory=set)  # must be listed in one realizes


def domain_ids(doc: dict[str, Any]) -> tuple[DomainIds, list[tuple[str, str]]]:
    ids = DomainIds()
    seen: list[tuple[str, str]] = []

    def add(id_: str, path: str, bucket: set[str] | None) -> None:
        seen.append((id_, path))
        ids.all.setdefault(id_, path)
        if bucket is not None:
            bucket.add(id_)

    for i, e in enumerate(doc.get("entities", [])):
        for j, inv in enumerate(e.get("invariants", [])):
            add(inv["id"], f"entities[{i}].invariants[{j}].id", ids.explicit)
    for i, r in enumerate(doc.get("relations", [])):
        for j, rule in enumerate(r.get("rules", [])):
            add(rule["id"], f"relations[{i}].rules[{j}].id", ids.explicit)
    for i, c in enumerate(doc.get("constraints", [])):
        add(c["id"], f"constraints[{i}].id", ids.explicit)
    for i, op in enumerate(doc.get("operations", [])):
        add(op["id"], f"operations[{i}].id", ids.operations)
        for j, post in enumerate(op.get("post", [])):
            add(post["id"], f"operations[{i}].post[{j}].id", ids.inherited)
        for j, err in enumerate(op.get("errors", [])):
            add(err["id"], f"operations[{i}].errors[{j}].id", ids.inherited)
    for i, p in enumerate(doc.get("properties", [])):
        single = len(p.get("operations", [])) == 1
        add(p["id"], f"properties[{i}].id", ids.inherited if single else ids.explicit)
    return ids, seen


def _domain_semantics(doc: dict[str, Any]) -> Iterator[Problem]:
    ids, seen = domain_ids(doc)
    yield from _duplicates(seen, "id")

    names = [(e["name"], f"entities[{i}].name") for i, e in enumerate(doc.get("entities", []))]
    names += [(e["name"], f"events[{i}].name") for i, e in enumerate(doc.get("events", []))]
    yield from _duplicates(names, "name")
    entities = {e["name"] for e in doc.get("entities", [])}
    events = {e["name"] for e in doc.get("events", [])}

    def refs(values: list[str], known: set[str], path: str, what: str) -> Iterator[Problem]:
        for k, value in enumerate(values):
            if value not in known:
                yield Problem(f"{path}[{k}]", f"no {what} named {value!r}")

    for key in ("entities", "events"):
        for i, owner in enumerate(doc.get(key, [])):
            fields = [(f["name"], f"{key}[{i}].fields[{j}].name") for j, f in enumerate(owner.get("fields", []))]
            yield from _duplicates(fields, "field")
    for i, r in enumerate(doc.get("relations", [])):
        for end in ("from", "to"):
            if r[end] not in entities:
                yield Problem(f"relations[{i}].{end}", f"no entity named {r[end]!r}")
    for i, c in enumerate(doc.get("constraints", [])):
        yield from refs(c["about"], entities, f"constraints[{i}].about", "entity")
    for i, op in enumerate(doc.get("operations", [])):
        yield from refs(op["uses"], entities, f"operations[{i}].uses", "entity")
        yield from refs(op.get("emits", []), events, f"operations[{i}].emits", "event")
    for i, p in enumerate(doc.get("properties", [])):
        yield from refs(p.get("operations", []), ids.operations, f"properties[{i}].operations", "operation")


def validate_domain(doc: Any) -> list[Problem]:
    return _schema_problems("domain", doc) or list(_domain_semantics(doc))


# --- architecture -----------------------------------------------------------


def _walk(elements: list[dict[str, Any]], path: str) -> Iterator[tuple[dict[str, Any], str]]:
    for i, element in enumerate(elements):
        here = f"{path}[{i}]"
        yield element, here
        yield from _walk(element.get("elements", []), f"{here}.elements")


def _assertion_refs(body: dict[str, Any], path: str, scope: tuple[str, ...]) -> Iterator[tuple[str, str, tuple[str, ...], str]]:
    """Yield (ref, path, variables in scope, role) for every reference of an assertion."""
    if "no_dependency" in body:
        for end in ("from", "to"):
            yield body["no_dependency"][end], f"{path}.no_dependency.{end}", scope, "element"
    if "only_depends_on" in body:
        yield body["only_depends_on"]["from"], f"{path}.only_depends_on.from", scope, "element"
        for k, ref in enumerate(body["only_depends_on"]["to"]):
            yield ref, f"{path}.only_depends_on.to[{k}]", scope, "element"
    if "foreach" in body:
        loop = body["foreach"]
        if "within" in loop:
            yield loop["within"], f"{path}.foreach.within", scope, "container"
        for k, ref in enumerate(loop.get("except", [])):
            yield ref, f"{path}.foreach.except[{k}]", scope, "element"
        yield from _assertion_refs(loop["assert"], f"{path}.foreach.assert", scope + (loop["as"],))


def _architecture_semantics(doc: dict[str, Any], domain: DomainIds) -> Iterator[Problem]:
    elements = list(_walk(doc["elements"], "elements"))
    by_id = {e["id"]: e for e, _ in elements}

    own = [(doc["system"]["id"], "system.id")] + [(e["id"], f"{p}.id") for e, p in elements]
    own += [(c["id"], f"constants[{i}].id") for i, c in enumerate(doc.get("constants", []))]
    own += [(a["id"], f"assertions[{i}].id") for i, a in enumerate(doc.get("assertions", []))]
    yield from _duplicates(own, "id")
    for id_, path in own:
        if id_ in domain.all:
            yield Problem(path, f"id {id_!r} is already a domain id ({domain.all[id_]})")

    # depends_on: existing components or libraries, no self, no cycle
    graph: dict[str, list[str]] = {}
    for e, path in elements:
        if e["kind"] != "component":
            continue
        graph[e["id"]] = []
        for k, dep in enumerate(e.get("depends_on", [])):
            target = by_id.get(dep)
            if target is None or target["kind"] not in ("component", "library"):
                yield Problem(f"{path}.depends_on[{k}]", f"no component or library with id {dep!r}")
            elif dep == e["id"]:
                yield Problem(f"{path}.depends_on[{k}]", "a component cannot depend on itself")
            elif target["kind"] == "component":
                graph[e["id"]].append(dep)
    yield from _cycles(graph, {e["id"]: p for e, p in elements})

    # assertions: references and variables
    for i, a in enumerate(doc.get("assertions", [])):
        for ref, path, scope, role in _assertion_refs(a, f"assertions[{i}]", ()):
            if ref.startswith("$"):
                if ref[1:] not in scope:
                    yield Problem(path, f"{ref} is not a variable of an enclosing foreach")
            elif ref not in by_id:
                yield Problem(path, f"no element with id {ref!r}")
            elif role == "container" and "elements" not in by_id[ref]:
                yield Problem(path, f"element {ref!r} contains no elements")

    # realization: every operation and explicit clause exactly once
    realized: list[tuple[str, str]] = []
    for e, path in elements:
        realized += [(r, f"{path}.realizes[{k}]") for k, r in enumerate(e.get("realizes", []))]
    if "integration" in doc:
        realized += [(r, f"integration.realizes[{k}]") for k, r in enumerate(doc["integration"]["realizes"])]
    for id_, path in realized:
        if id_ in domain.inherited:
            yield Problem(path, f"{id_!r} comes with its operation and is never listed")
        elif id_ not in domain.operations and id_ not in domain.explicit:
            yield Problem(path, f"no domain id {id_!r}")
    yield from _duplicates([(i, p) for i, p in realized if i in domain.operations | domain.explicit], "realized id")
    listed = {i for i, _ in realized}
    for id_ in sorted((domain.operations | domain.explicit) - listed):
        yield Problem(domain.all[id_], f"{id_!r} is realized by no component")


def _cycles(graph: dict[str, list[str]], paths: dict[str, str]) -> Iterator[Problem]:
    state: dict[str, int] = {}  # 1 visiting, 2 done
    stack: list[str] = []

    def visit(node: str) -> Iterator[Problem]:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                cycle = stack[stack.index(nxt):] + [nxt]
                yield Problem(f"{paths[node]}.depends_on", "dependency cycle: " + " -> ".join(cycle))
            elif nxt not in state:
                yield from visit(nxt)
        stack.pop()
        state[node] = 2

    for node in graph:
        if node not in state:
            yield from visit(node)


def validate_architecture(doc: Any, domain_doc: Any) -> list[Problem]:
    problems = _schema_problems("architecture", doc)
    if problems:
        return problems
    if validate_domain(domain_doc):
        return [Problem("", "the domain is not valid: validate it first")]
    ids, _ = domain_ids(domain_doc)
    return list(_architecture_semantics(doc, ids))
