"""BuildIndex: the two DSLs and the code of a project into .beck/index.db."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beck.dsl.validate import Problem, validate_architecture, validate_domain
from beck.index.anchor import Marker, anchor
from beck.index.graph import load_architecture, load_domain
from beck.index.store import create


@dataclass
class BuildReport:
    problems: list[Problem] = field(default_factory=list)  # the DSLs are not valid: no index
    files: int = 0
    markers: int = 0
    unanchored: list[str] = field(default_factory=list)  # operations no symbol cites (AnchorIds, kent)
    unknown: list[Marker] = field(default_factory=list)  # markers citing an id the DSLs lack


def build(root: Path, domain: Any, architecture: Any, db: Path | str) -> tuple[sqlite3.Connection | None, BuildReport]:
    report = BuildReport(problems=validate_domain(domain) or validate_architecture(architecture, domain))
    if report.problems:
        return None, report
    conn = create(db)
    with conn:
        load_domain(conn, domain)
        load_architecture(conn, architecture)
        anchored = anchor(conn, root)
    report.files, report.markers, report.unknown = anchored.files, anchored.markers, anchored.unknown
    report.unanchored = [r["id"] for r in conn.execute(
        "SELECT id FROM node n WHERE kind = 'operation' AND NOT EXISTS "
        "(SELECT 1 FROM edge e WHERE e.src = n.id AND e.kind = 'anchored_to') ORDER BY rowid")]
    return conn, report
