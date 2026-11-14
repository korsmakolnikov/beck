"""The JUnit report of a test run, read deterministically, and the compact summary the agent reads.

The agent never reads the runner's console output: it reads `ALL_OK` when
every test passed, or one entry per test that did not pass (its id, the kind
of failure, the first lines of the message, the file and line of the
failure). The full output stays in the run's log, for whoever needs it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

MESSAGE_LINES = 3
MESSAGE_CHARS = 300


@dataclass(frozen=True)
class Case:
    id: str
    outcome: str                 # passed | failure | error | skipped
    message: str = ""
    location: str | None = None  # file:line of the failure, inside the project


@dataclass(frozen=True)
class Report:
    cases: tuple[Case, ...]

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.cases if c.outcome == outcome)

    @property
    def not_passed(self) -> list[Case]:
        return [c for c in self.cases if c.outcome in ("failure", "error")]

    def counts(self) -> dict[str, int]:
        return {"tests": len(self.cases), **{k: self.count(k) for k in ("passed", "failure", "error", "skipped")}}


class UnreadableReport(ValueError):
    pass


def parse(path: Path, root: Path) -> Report:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as error:
        raise UnreadableReport(f"{path}: {error}") from error
    cases = []
    for tc in tree.iter("testcase"):
        outcome, message, location = "passed", "", None
        for kind in ("failure", "error", "skipped"):
            node = tc.find(kind)
            if node is not None:
                outcome = kind
                text = (node.get("message") or "") + "\n" + (node.text or "")
                message = _short(node.get("message") or node.text or "")
                location = _location(text, root, tc.get("file"), tc.get("line"))
                break
        cases.append(Case(_id(tc, root), outcome, message, location))
    return Report(tuple(cases))


def _id(tc: ET.Element, root: Path) -> str:
    """pytest's `path::name` when the file can be found, else `classname::name`."""
    name, classname, file = tc.get("name", "?"), tc.get("classname", ""), tc.get("file")
    if file:
        return f"{file}::{name}"
    parts = [p for p in classname.split(".") if p]
    for cut in range(len(parts), 0, -1):  # tests.test_a.TestX -> tests/test_a.py::TestX::name
        candidate = Path(*parts[:cut]).with_suffix(".py")
        if (root / candidate).is_file():
            return "::".join([str(candidate), *parts[cut:], name])
    return f"{classname}::{name}" if classname else name


_LOCATION = re.compile(r"([\w./-]+\.(?:py|exs?|c|h)):(\d+)")


def _location(text: str, root: Path, file: str | None, line: str | None) -> str | None:
    found = [f"{m.group(1)}:{m.group(2)}" for m in _LOCATION.finditer(text) if (root / m.group(1)).is_file()]
    if found:
        return found[-1]  # the innermost frame inside the project
    return f"{file}:{line}" if file and line else None


def _short(message: str) -> str:
    lines = [line.rstrip() for line in message.strip().splitlines() if line.strip()][:MESSAGE_LINES]
    text = "\n".join(lines)
    return text if len(text) <= MESSAGE_CHARS else text[:MESSAGE_CHARS] + "…"


def summary(report: Report) -> str:
    """What the agent reads."""
    if not report.not_passed:
        skipped = report.count("skipped")
        return f"ALL_OK ({report.count('passed')} passed" + (f", {skipped} skipped)" if skipped else ")")
    lines = [f"NOT_OK ({len(report.not_passed)} of {len(report.cases)} not passed)"]
    for c in report.not_passed:
        lines.append(f"- {c.id} [{c.outcome}]" + (f" at {c.location}" if c.location else ""))
        lines.extend(f"    {line}" for line in c.message.splitlines())
    return "\n".join(lines)
