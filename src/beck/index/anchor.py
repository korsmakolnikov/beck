"""Anchoring: the `spec: <id>` markers of the code, read with tree-sitter.

A marker in a source file anchors its id to a symbol (`anchored_to`); a marker
in a test file makes the test prove the id (`tests`). The symbol is the
innermost definition holding the marker, or, when the marker sits in a
statement before a definition (a comment, an Elixir `@doc`, a decorator), the
definition that follows it.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tree_sitter as ts
import tree_sitter_c
import tree_sitter_elixir
import tree_sitter_python

from beck.index.store import add_edge, add_node

MARKER = re.compile(rb"spec:\s*([a-z][a-z0-9]*(?:-[a-z0-9]+)*)")


@dataclass(frozen=True)
class Language:
    grammar: ts.Language
    is_definition: Callable[[ts.Node], bool]
    name: Callable[[ts.Node], str]


def _text(node: ts.Node | None) -> str:
    return node.text.decode() if node is not None and node.text is not None else "?"


# --- python ---

def _py_name(node: ts.Node) -> str:
    return _text(node.child_by_field_name("name"))


# --- elixir: definitions are calls (def, test, ...) and @callback attributes ---

_EX_DEFS = {"def", "defp", "defmacro", "defmacrop", "defmodule", "defprotocol", "defimpl",
            "test", "describe", "property"}


def _ex_call(node: ts.Node) -> tuple[str, ts.Node | None] | None:
    """(keyword, its arguments) of `def …`, `test …` or `@callback …`, else None."""
    if node.type == "unary_operator" and node.child_count == 2 and node.children[1].type == "call":
        node = node.children[1]
        keyword = {"callback": "callback", "macrocallback": "macrocallback"}
    else:
        keyword = None
    if node.type != "call" or node.child_count == 0 or node.children[0].type != "identifier":
        return None
    word = _text(node.children[0])
    if (keyword is not None and word not in keyword) or (keyword is None and word not in _EX_DEFS):
        return None
    args = next((c for c in node.children if c.type == "arguments"), None)
    return word, args


def _ex_name(node: ts.Node) -> str:
    word, args = _ex_call(node) or ("?", None)
    head = args.named_children[0] if args is not None and args.named_children else None
    while head is not None and head.type == "binary_operator":  # `f(x) when …`, `f(x) :: t`
        head = head.child_by_field_name("left") or head.named_children[0]
    if head is None:
        return word
    if head.type == "call" and head.children and head.children[0].type == "identifier":
        call_args = next((c for c in head.children if c.type == "arguments"), None)
        arity = len(call_args.named_children) if call_args is not None else 0
        return f"{word} {_text(head.children[0])}/{arity}"
    if head.type == "string":
        return f"{word} {_text(head)}"
    return f"{word} {_text(head)}"


# --- c: function definitions and prototypes ---

def _c_is_definition(node: ts.Node) -> bool:
    if node.type == "function_definition":
        return True
    return node.type == "declaration" and _c_declarator(node) is not None


def _c_declarator(node: ts.Node) -> ts.Node | None:
    d = node.child_by_field_name("declarator")
    while d is not None and d.type != "function_declarator":
        d = d.child_by_field_name("declarator")
    return d


def _c_name(node: ts.Node) -> str:
    d = _c_declarator(node)
    return _text(d.child_by_field_name("declarator")) if d is not None else "?"


LANGUAGES = {
    ".py": Language(ts.Language(tree_sitter_python.language()),
                    lambda n: n.type in ("function_definition", "class_definition"), _py_name),
    ".ex": Language(ts.Language(tree_sitter_elixir.language()), lambda n: _ex_call(n) is not None, _ex_name),
    ".c": Language(ts.Language(tree_sitter_c.language()), _c_is_definition, _c_name),
}
LANGUAGES[".exs"] = LANGUAGES[".ex"]
LANGUAGES[".h"] = LANGUAGES[".c"]


def is_test_file(path: Path) -> bool:
    name = path.name
    return (any(part in ("test", "tests") for part in path.parts[:-1])
            or name.startswith("test_") or path.stem.endswith("_test"))


def project_files(root: Path) -> list[Path]:
    """The project's files beck reads: git's when it is a repository, else all of them."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=root, capture_output=True, text=True, check=True).stdout
        paths = [Path(line) for line in out.splitlines()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = [p.relative_to(root) for p in root.rglob("*") if p.is_file()]
    return sorted(p for p in paths if p.suffix in LANGUAGES and ".beck" not in p.parts
                  and (root / p).is_file())


@dataclass(frozen=True)
class Marker:
    id: str
    path: str
    line: int
    symbol: str          # node id of the symbol or test
    signature: str
    is_test: bool


# statements that may stand right before the definition they document
_BEFORE = {"comment", "decorator", "unary_operator"}


def _symbol_for(root: ts.Node, offset: int, lang: Language) -> list[ts.Node]:
    """The chain of definitions down to the one the marker at offset belongs to."""
    chain: list[ts.Node] = []
    node = root
    while (child := next((c for c in node.named_children if c.start_byte <= offset < c.end_byte), None)):
        if lang.is_definition(child):
            chain.append(child)
        elif child.type in _BEFORE and (follower := _next_definition(node, child, lang)) is not None:
            chain.append(follower)
            break
        node = child
    return chain


def _next_definition(parent: ts.Node, child: ts.Node, lang: Language) -> ts.Node | None:
    """The definition right after child, past other comments, attributes and decorators."""
    for sibling in parent.named_children:
        if sibling.start_byte < child.end_byte:
            continue
        if sibling.type == "decorated_definition":  # python: the definition under its decorators
            sibling = sibling.child_by_field_name("definition") or sibling
        if lang.is_definition(sibling):
            return sibling
        if sibling.type not in _BEFORE:
            return None
    return None


def scan(root: Path, path: Path) -> list[Marker]:
    source = (root / path).read_bytes()
    if b"spec:" not in source:
        return []
    lang = LANGUAGES[path.suffix]
    tree = ts.Parser(lang.grammar).parse(source)
    test = is_test_file(path)
    markers = []
    for match in MARKER.finditer(source):
        chain = _symbol_for(tree.root_node, match.start(), lang)
        names = [lang.name(n) for n in chain] or ["<file>"]
        target = chain[-1] if chain else None
        signature = _text(target).splitlines()[0] if target is not None else ""
        markers.append(Marker(match.group(1).decode(), str(path), source.count(b"\n", 0, match.start()) + 1,
                              f"{path}::{'.'.join(names)}", signature, test))
    return markers


@dataclass
class AnchorReport:
    files: int = 0
    markers: int = 0
    unknown: list[Marker] = field(default_factory=list)  # markers citing an id the DSLs do not have


def anchor(conn: sqlite3.Connection, root: Path) -> AnchorReport:
    known = {row["id"] for row in conn.execute("SELECT id FROM node WHERE kind IN ('operation', 'clause')")}
    report = AnchorReport()
    for path in project_files(root):
        content = (root / path).read_bytes()
        conn.execute("INSERT INTO file (path, hash) VALUES (?, ?)", (str(path), hashlib.sha256(content).hexdigest()))
        report.files += 1
        for m in scan(root, path):
            report.markers += 1
            if m.id not in known:
                report.unknown.append(m)
                continue
            kind = "test" if m.is_test else "symbol"
            if conn.execute("SELECT 1 FROM node WHERE id = ?", (m.symbol,)).fetchone() is None:
                add_node(conn, m.symbol, kind, text=m.signature, file=m.path, line=m.line)
            if m.is_test:
                add_edge(conn, m.symbol, m.id, "tests")
            else:
                add_edge(conn, m.id, m.symbol, "anchored_to")
    return report


def ids_at(root: Path, path: Path, lines: set[int]) -> dict[int, list[str]]:
    """The ids of the markers that hold each line: those of its innermost definition, then of the enclosing ones.

    A line outside every marked definition holds no id. Used to hand a line no test runs to the clause
    that names its code.
    """
    if path.suffix not in LANGUAGES or not (root / path).is_file():
        return {}
    source = (root / path).read_bytes()
    lang = LANGUAGES[path.suffix]
    tree = ts.Parser(lang.grammar).parse(source)
    by_symbol: dict[str, list[str]] = {}
    for m in scan(root, path):
        by_symbol.setdefault(m.symbol, []).append(m.id)
    spans: list[tuple[int, int, list[str]]] = []

    def walk(node: ts.Node, names: list[str], before: list[str]) -> None:
        for child in node.named_children:
            if lang.is_definition(child):
                chain = [*names, lang.name(child)]
                ids = by_symbol.get(f"{path}::{'.'.join(chain)}", []) + before
                if ids:
                    spans.append((child.start_point[0] + 1, child.end_point[0] + 1, ids))
                walk(child, chain, ids)
            else:
                walk(child, names, before)

    walk(tree.root_node, [], by_symbol.get(f"{path}::<file>", []))
    out: dict[int, list[str]] = {}
    for line in lines:
        holding = [s for s in spans if s[0] <= line <= s[1]]
        if holding:
            out[line] = list(dict.fromkeys(min(holding, key=lambda s: s[1] - s[0])[2]))
    return out
