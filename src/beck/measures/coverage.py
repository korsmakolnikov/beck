"""The coverage reports, behind one interface: `read(report, root) -> {file: FileCoverage}`.

`toolchain.yml` names the report's format (`coverage: {format: …}`):

* `coverage.py`: coverage.py's JSON (`pytest --cov-report=json:…`);
* `excoveralls`: ExCoveralls' JSON (`mix coveralls.json`), a hit count or
  null per line;
* `lcov`: the lcov tracefile (`DA:<line>,<hits>`), which gcovr, lcov,
  c8, excoveralls (`coveralls.lcov`) and most other tools write.

Files are named relative to the root the tests ran in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class FileCoverage:
    executed: frozenset[int] = field(default_factory=frozenset)
    missing: frozenset[int] = field(default_factory=frozenset)

    @property
    def lines(self) -> int:
        return len(self.executed | self.missing)


Reader = Callable[[Path, Path], dict[str, FileCoverage]]


def _relative(name: str, root: Path) -> str:
    p = Path(name)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            return name
    return str(p)


def coverage_py(report: Path, root: Path) -> dict[str, FileCoverage]:
    data = json.loads(report.read_text())["files"]
    return {_relative(name, root): FileCoverage(frozenset(v["executed_lines"]), frozenset(v["missing_lines"]))
            for name, v in data.items()}


def excoveralls(report: Path, root: Path) -> dict[str, FileCoverage]:
    out = {}
    for source in json.loads(report.read_text())["source_files"]:
        hits = source["coverage"]
        out[_relative(source["name"], root)] = FileCoverage(
            frozenset(i for i, h in enumerate(hits, 1) if h), frozenset(i for i, h in enumerate(hits, 1) if h == 0))
    return out


def lcov(report: Path, root: Path) -> dict[str, FileCoverage]:
    out: dict[str, FileCoverage] = {}
    name: str | None = None
    executed: set[int] = set()
    missing: set[int] = set()
    for line in report.read_text().splitlines():
        if line.startswith("SF:"):
            name, executed, missing = line[3:], set(), set()
        elif line.startswith("DA:") and name is not None:
            number, hits = line[3:].split(",")[:2]
            (executed if int(hits) > 0 else missing).add(int(number))
        elif line == "end_of_record" and name is not None:
            out[_relative(name, root)] = FileCoverage(frozenset(executed), frozenset(missing - executed))
            name = None
    return out


READERS: dict[str, Reader] = {"coverage.py": coverage_py, "excoveralls": excoveralls, "lcov": lcov}
