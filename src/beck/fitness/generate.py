"""GenerateFitnessFunctions: the architecture's assertions into a table of test cases.

docs/dsl.md, "Assertions". Every structured assertion is expanded into
concrete cases (a `foreach` into one case per element); every element gets a
`defined` case, so that no assertion passes on an empty tree; every component
gets the implicit `<id>-declared-dependencies` case. A `rule` is not
translated: it is reported, for kent to write (WriteFitnessFunctions).

Expects an architecture that `beck validate` accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

TEMPLATES = files("beck.fitness") / "templates"


@dataclass(frozen=True)
class Case:
    id: str
    form: str                  # defined | contained_in | no_dependency | only_depends_on
    origin: str | None         # an element id
    targets: tuple[str, ...]   # element ids
    allow: tuple[str, ...] = ()  # element ids exempt from contained_in


@dataclass(frozen=True)
class Element:
    id: str
    kind: str
    namespace: tuple[str, ...]   # the physical names from the system down; an external library's own
    parent: str | None
    children: tuple[str, ...]
    external: bool = False       # a library with a package: outside the system


@dataclass
class Expansion:
    system: Element
    elements: dict[str, Element]
    cases: list[Case] = field(default_factory=list)
    rules: dict[str, str] = field(default_factory=dict)   # untranslated: id -> sentence


def _elements(arch: dict[str, Any]) -> tuple[Element, dict[str, Element]]:
    system = arch["system"]
    out: dict[str, Element] = {}

    def walk(e: dict[str, Any], parent: str, namespace: tuple[str, ...]) -> None:
        external = e["kind"] == "library" and "package" in e
        here = (e["physical"],) if external else namespace + (e["physical"],)
        out[e["id"]] = Element(e["id"], e["kind"], here, parent, tuple(c["id"] for c in e.get("elements", [])),
                               external)
        for c in e.get("elements", []):
            walk(c, e["id"], here)

    root = (system["physical"],)
    for e in arch["elements"]:
        walk(e, system["id"], root)
    top = Element(system["id"], "system", root, None, tuple(e["id"] for e in arch["elements"]))
    return top, out


def _descendants(elements: dict[str, Element], id_: str) -> list[str]:
    out = []
    for child in elements[id_].children:
        out.append(child)
        out.extend(_descendants(elements, child))
    return out


def expand(arch: dict[str, Any]) -> Expansion:
    system, elements = _elements(arch)
    x = Expansion(system, elements)
    for e in elements.values():
        if not e.external:
            x.cases.append(Case(f"{e.id}-is-defined", "defined", None, (e.id,)))
    for c in _components(arch):
        x.cases.append(Case(f"{c['id']}-declared-dependencies", "only_depends_on", c["id"],
                            tuple(c.get("depends_on", []))))
    for a in arch.get("assertions", []):
        if "rule" in a:
            x.rules[a["id"]] = a["rule"]
        else:
            x.cases.extend(_expand(x, a, a["id"], {}))
    ids = [c.id for c in x.cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"assertion ids clash with the generated ones: {duplicates}")
    return x


def _components(arch: dict[str, Any]) -> list[dict[str, Any]]:
    out = []

    def walk(e: dict[str, Any]) -> None:
        if e["kind"] == "component":
            out.append(e)
        for c in e.get("elements", []):
            walk(c)

    for e in arch["elements"]:
        walk(e)
    return out


def _expand(x: Expansion, body: dict[str, Any], id_: str, env: dict[str, str]) -> list[Case]:
    ref = lambda r: env[r[1:]] if r.startswith("$") else r  # noqa: E731
    if "no_dependency" in body:
        a = body["no_dependency"]
        return [Case(id_, "no_dependency", ref(a["from"]), (ref(a["to"]),))]
    if "only_depends_on" in body:
        a = body["only_depends_on"]
        return [Case(id_, "only_depends_on", ref(a["from"]), tuple(ref(t) for t in a["to"]))]
    if "contained_in" in body:
        kinds = set(body["contained_in"]["within"])
        # a library is a leaf like a component: its modules are contained in it
        targets = tuple(e.id for e in x.elements.values()
                        if (e.kind in kinds or e.kind == "library") and not e.external)
        # the modules of the system and of the containers outside those kinds are allowed
        allow = (x.system.id,) + tuple(e.id for e in x.elements.values() if e.id not in targets and e.children)
        return [Case(id_, "contained_in", None, targets, allow)]
    loop = body["foreach"]
    scope = _descendants(x.elements, loop["within"]) if "within" in loop else list(x.elements)
    cases = []
    for element in scope:
        if x.elements[element].kind == loop["over"] and element not in loop.get("except", []):
            cases += _expand(x, loop["assert"], f"{id_}[{element}]", {**env, loop["as"]: element})
    return cases


# --- rendering ---------------------------------------------------------------


def camelize(physical: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in physical.split("_"))


@dataclass(frozen=True)
class Written:
    stack: str
    files: list[str]
    rules_file: str
    pending_rules: dict[str, str]   # rules not yet cited (spec: <id>) in the rules file
    notes: list[str]


def detect_stack(root: Path) -> str:
    if (root / "mix.exs").is_file():
        return "elixir"
    if (root / "pyproject.toml").is_file():
        return "python"
    raise ValueError("unsupported stack: no mix.exs or pyproject.toml (C is not supported yet)")


def _python(x: Expansion, root: Path) -> tuple[dict[str, str], str, list[str]]:
    ns = lambda id_: ".".join((x.elements.get(id_) or x.system).namespace)  # noqa: E731
    source_root = "src" if (root / "src").is_dir() else "."
    tests = "tests" if (root / "tests").is_dir() or not (root / "test").is_dir() else "test"
    lines = []
    for c in x.cases:
        origin = repr(ns(c.origin)) if c.origin else "None"
        targets = [ns(t) for t in c.targets]
        allow = [ns(a) for a in c.allow]
        lines.append(f"    ({c.id!r}, {c.form!r}, {origin}, {targets!r}, {allow!r}),")
    text = f'''"""Fitness functions of .beck/architecture.yaml.

Generated by `beck fitness generate`; do not edit. The rules beck cannot
translate are written by kent in test_architecture_rules.py, beside this file.
"""

from pathlib import Path

import pytest

import beck_fitness

SOURCE_ROOT = Path(__file__).parents[2] / {source_root!r}
PACKAGE = {ns(x.system.id)!r}

# (id, form, origin, targets, allow)
CASES = [
{chr(10).join(lines)}
]


@pytest.mark.parametrize("form, origin, targets, allow", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_architecture(form, origin, targets, allow):
    beck_fitness.check(SOURCE_ROOT, PACKAGE, form, origin, targets, allow)
'''
    out = {f"{tests}/fitness/test_architecture.py": text,
           f"{tests}/fitness/beck_fitness.py": (TEMPLATES / "beck_fitness.py").read_text()}
    return out, f"{tests}/fitness/test_architecture_rules.py", []


def _elixir(x: Expansion, root: Path) -> tuple[dict[str, str], str, list[str]]:
    ns = lambda id_: ".".join(camelize(p) for p in (x.elements.get(id_) or x.system).namespace)  # noqa: E731
    app = x.system.namespace[0]
    lines = []
    for c in x.cases:
        origin = ns(c.origin) if c.origin else "nil"
        targets = ", ".join(ns(t) for t in c.targets)
        allow = ", ".join(ns(a) for a in c.allow)
        lines.append(f'    {{"{c.id}", {{:{c.form}, {origin}, [{targets}], [{allow}]}}}},')
    lines[-1] = lines[-1].rstrip(",")
    text = f'''defmodule {ns(x.system.id)}.ArchitectureTest do
  @moduledoc """
  Fitness functions of .beck/architecture.yaml.

  Generated by `beck fitness generate`; do not edit. The rules beck cannot
  translate are written by kent in architecture_rules_test.exs.
  """
  use ExUnit.Case, async: true

  @app :{app}
  @root {ns(x.system.id)}

  # {{id, {{form, from, targets, allow}}}}
  @cases [
{chr(10).join(lines)}
  ]

  for {{id, assertion}} <- @cases do
    test id do
      BeckFitness.check(@app, @root, unquote(Macro.escape(assertion)))
    end
  end
end
'''
    out = {"test/fitness/architecture_test.exs": text,
           f"{ELIXIR_SUPPORT}/beck_fitness.ex": (TEMPLATES / "beck_fitness.ex").read_text()}
    notes = []
    mix = root / "mix.exs"
    if mix.is_file():
        patched = compile_support(mix.read_text())
        if patched is None:
            notes.append(f"mix.exs: could not add {ELIXIR_SUPPORT} to elixirc_paths in the test env; add it by hand")
        elif patched != mix.read_text():
            out["mix.exs"] = patched
    return out, "test/fitness/architecture_rules_test.exs", notes


ELIXIR_SUPPORT = "test/fitness/support"


def compile_support(mix: str) -> str | None:
    """mix.exs with ELIXIR_SUPPORT compiled in the test env; None when its shape is not recognised."""
    if f'"{ELIXIR_SUPPORT}"' in mix:
        return mix
    test_clause = re.compile(r"(defp elixirc_paths\(:test\),\s*do:\s*\[)([^\]]*)\]")
    if test_clause.search(mix):
        return test_clause.sub(lambda m: f'{m.group(1)}{m.group(2).rstrip()}, "{ELIXIR_SUPPORT}"]', mix, count=1)
    if "elixirc_paths" in mix:
        return None
    entry = "elixirc_paths: elixirc_paths(Mix.env()),"
    block = re.compile(r"(def project do\s*\n(\s*)\[)")
    inline = re.compile(r"(def project,\s*do:\s*\[)")
    if block.search(mix):
        mix = block.sub(lambda m: f"{m.group(1)}\n{m.group(2)}  {entry}", mix, count=1)
    elif inline.search(mix):
        mix = inline.sub(lambda m: f"{m.group(1)}{entry} ", mix, count=1)
    else:
        return None
    last_end = mix.rstrip().rfind("end")
    clauses = (f'\n  defp elixirc_paths(:test), do: ["lib", "{ELIXIR_SUPPORT}"]\n'
               '  defp elixirc_paths(_), do: ["lib"]\n')
    return mix[:last_end].rstrip() + "\n" + clauses + mix[last_end:]



def write(root: Path, arch: dict[str, Any], stack: str | None = None) -> Written:
    stack = stack or detect_stack(root)
    x = expand(arch)
    render = {"python": _python, "elixir": _elixir}[stack]
    out, rules_file, notes = render(x, root)
    for path, text in out.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    rules_path = root / rules_file
    written = rules_path.read_text() if rules_path.is_file() else ""
    pending = {i: r for i, r in x.rules.items() if not re.search(rf"spec:\s*{re.escape(i)}\b", written)}
    return Written(stack, sorted(out), rules_file, pending, notes)
