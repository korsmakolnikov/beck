"""The complexity of the functions, behind one interface: `functions(root, files) -> [Function]`.

Each backend measures the languages it knows; `toolchain.yml` names the
backend (`measures: {complexity: …}`), with a default per language:

* `lizard`: the Python library (Python, C, Go, Rust, Java, JavaScript, …);
* `credo`: Elixir's own linter, the community's definition of the
  complexity. Credo only checks a threshold, so beck runs its
  CyclomaticComplexity check alone with the threshold at 0, which reports
  every function with its value. The functions themselves (their size,
  parameters, length) come from the tree-sitter backend, which also stands
  in for any function Credo does not report, or for all of them when the
  project has no Credo;
* `elixir`: beck's own, on tree-sitter-elixir: lizard has no Elixir.

The cyclomatic complexity of a function is 1 plus its decision points. For
Elixir: `if` and `unless`, a guard (`when`), every `<-` of a `with`,
`and`, `or`, `&&`, `||`, every clause of a `case`, `cond`, `receive`, `fn`
or `else` past the first, every clause of a `rescue` or `catch`. Each
clause of a multi-clause function is a function of its own, as lizard
counts the branches of one body.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import lizard
import tree_sitter as ts
import tree_sitter_elixir

if TYPE_CHECKING:
    from beck.toolchain import Toolchain


@dataclass(frozen=True)
class Function:
    file: str
    name: str
    line: int
    ccn: int
    nloc: int        # lines of code, blank lines and comments apart
    params: int
    length: int      # lines, from the head to the end


Backend = Callable[[Path, list[str], "Toolchain | None"], list[Function]]

LIZARD_SUFFIXES = {".py", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".java", ".js", ".ts", ".erl", ".rb"}


def by_lizard(root: Path, files: list[str], toolchain: Toolchain | None = None) -> list[Function]:
    found = []
    for f in files:
        if Path(f).suffix not in LIZARD_SUFFIXES:
            continue
        for fn in lizard.analyze_file(str(root / f)).function_list:
            found.append(Function(f, fn.name, fn.start_line, fn.cyclomatic_complexity, fn.nloc,
                                  fn.parameter_count, fn.end_line - fn.start_line + 1))
    return found


_ELIXIR = ts.Language(tree_sitter_elixir.language())
_DEFS = {"def", "defp", "defmacro", "defmacrop"}
_BRANCHES = {"if", "unless"}
_BOOLEAN = {"and", "or", "&&", "||"}
_EVERY_CLAUSE = {"rescue_block", "catch_block"}


def _identifier(call: ts.Node) -> str | None:
    target = call.named_children[0] if call.named_children else None
    return target.text.decode() if target is not None and target.type == "identifier" and target.text else None


def _head(definition: ts.Node) -> tuple[str, int, bool]:
    """The name, the arity and whether a guard holds, of a `def` call."""
    args = next((c for c in definition.named_children if c.type == "arguments"), None)
    head = args.named_children[0] if args is not None and args.named_children else None
    guarded = False
    if head is not None and head.type == "binary_operator" and _operator(head) == "when":
        guarded, head = True, head.named_children[0]
    if head is None:
        return "?", 0, guarded
    if head.type == "call":
        inner = next((c for c in head.named_children if c.type == "arguments"), None)
        return _identifier(head) or "?", len(inner.named_children) if inner is not None else 0, guarded
    return (head.text or b"?").decode(), 0, guarded


def _operator(node: ts.Node) -> str:
    op = node.child_by_field_name("operator")
    return op.text.decode() if op is not None and op.text else ""


def _decisions(node: ts.Node) -> int:
    n = 0
    if node.type == "call" and _identifier(node) in _BRANCHES:
        n += 1
    elif node.type == "binary_operator" and _operator(node) in _BOOLEAN | {"<-"}:
        n += 1
    clauses = sum(1 for c in node.named_children if c.type == "stab_clause")
    if clauses:
        n += clauses if node.type in _EVERY_CLAUSE else clauses - 1
    return n + sum(_decisions(c) for c in node.named_children)


def by_elixir(root: Path, files: list[str], toolchain: Toolchain | None = None) -> list[Function]:
    found = []
    parser = ts.Parser(_ELIXIR)
    for f in files:
        if Path(f).suffix not in (".ex", ".exs"):
            continue
        source = (root / f).read_bytes()
        lines = source.decode(errors="replace").splitlines()
        stack = [parser.parse(source).root_node]
        while stack:
            node = stack.pop()
            if node.type == "call" and _identifier(node) in _DEFS:
                name, arity, guarded = _head(node)
                first, last = node.start_point[0], node.end_point[0]
                body = lines[first:last + 1]
                nloc = sum(1 for line in body if line.strip() and not line.strip().startswith("#"))
                found.append(Function(f, f"{name}/{arity}", first + 1, 1 + int(guarded) + _decisions(node),
                                      nloc, arity, last - first + 1))
                continue
            stack.extend(reversed(node.named_children))
    return sorted(found, key=lambda fn: (fn.file, fn.line))


CREDO_CONFIG = """%{configs: [%{name: "default", files: %{included: ["lib/", "test/"]},
  checks: %{enabled: [{Credo.Check.Refactor.CyclomaticComplexity, [max_complexity: 0]}]}}]}
"""
CREDO_CONFIG_PATH = ".beck/credo-complexity.exs"   # relative: also valid inside the toolchain's exec
_CC = re.compile(r"cyclomatic complexity is (\d+)")


def credo_complexity(root: Path, files: list[str], toolchain: Toolchain | None) -> dict[tuple[str, int], int]:
    """(file, line of the def) → Credo's cyclomatic complexity; empty when Credo cannot run."""
    if not files:
        return {}
    config = root / CREDO_CONFIG_PATH
    config.parent.mkdir(exist_ok=True)
    config.write_text(CREDO_CONFIG)
    template = ["mix", "credo", "--config-file", CREDO_CONFIG_PATH, "--format", "json", "{paths}"]
    argv, env = toolchain.argv(template, files) if toolchain else (template[:-1] + files, {})
    try:
        out = subprocess.run(argv, cwd=root, env={**os.environ, **env}, capture_output=True, text=True,
                             timeout=600).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    finally:
        config.unlink(missing_ok=True)
    start = out.find("{")
    try:
        issues = json.loads(out[start:])["issues"] if start >= 0 else []
    except (ValueError, KeyError, TypeError):
        return {}
    found = {}
    for issue in issues:
        m = _CC.search(str(issue.get("message", "")))
        if m and issue.get("filename") and issue.get("line_no"):
            found[(str(issue["filename"]), int(issue["line_no"]))] = int(m.group(1))
    return found


def by_credo(root: Path, files: list[str], toolchain: Toolchain | None = None) -> list[Function]:
    functions = by_elixir(root, files)
    credo = credo_complexity(root, sorted({f.file for f in functions}), toolchain)
    return [replace(f, ccn=credo.get((f.file, f.line), f.ccn)) for f in functions]


BACKENDS: dict[str, Backend] = {"lizard": by_lizard, "elixir": by_elixir, "credo": by_credo}
DEFAULT = {"python": "lizard", "elixir": "credo", "c": "lizard"}
