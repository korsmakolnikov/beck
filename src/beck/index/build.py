"""BuildIndex: the DSLs (the domain specs, the architecture) and the code of a project into .beck/index.db."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beck.dsl.domains import merge
from beck.dsl.validate import Problem, validate_architecture, validate_domains
from beck.index.anchor import Marker, anchor
from beck.index.graph import load_architecture, load_domain
from beck.index.store import create


@dataclass
class BuildReport:
    problems: list[Problem] = field(default_factory=list)  # the DSLs are not valid: no index
    files: int = 0
    markers: int = 0
    unanchored: list[str] = field(default_factory=list)  # morphisms no symbol references (AnchorIds, kent)
    unknown: list[Marker] = field(default_factory=list)  # markers referencing an id the DSLs lack


def build(root: Path, specs: dict[str, Any], architecture: Any,
          db: Path | str) -> tuple[sqlite3.Connection | None, BuildReport]:
    """The index of the domain specs (by domain), the architecture and the code under root."""
    report = BuildReport(problems=validate_domains(specs) or validate_architecture(architecture, specs))
    if report.problems:
        return None, report
    conn = create(db)
    with conn:
        load_domain(conn, merge(specs))
        load_architecture(conn, architecture)
        anchored = anchor(conn, root)
    report.files, report.markers, report.unknown = anchored.files, anchored.markers, anchored.unknown
    report.unanchored = [r["id"] for r in conn.execute(
        "SELECT id FROM node n WHERE kind = 'morphism' AND NOT EXISTS "
        "(SELECT 1 FROM edge e WHERE e.src = n.id AND e.kind = 'anchored_to') ORDER BY rowid")]
    return conn, report
