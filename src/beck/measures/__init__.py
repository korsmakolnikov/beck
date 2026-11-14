"""The measures of a unit, deterministic, each behind an interface the toolchain configures.

* Size and complexity (`complexity.py`): the functions of the unit's files,
  from the backend `toolchain.yml` names (lizard; Credo for Elixir, beck's own counter on tree-sitter as its fallback).
  A refactor worsens the unit when the complexity grows: the maximum or the
  mean of the functions' cyclomatic complexity. The size, the parameters and
  the length are reported for martin, never judged: extracting a function
  adds lines.
* Coverage (`coverage.py`): the report of the run, in the format the
  toolchain names, read into lines executed and missing per file. Two uses:
  the gaps, the lines the iteration changed (the diff from the unit's base)
  that no test ran, which beck tries once to cover and then hands to martin;
  and the ratchet: the project's coverage never goes down.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from beck.measures.complexity import BACKENDS, Function
from beck.measures.coverage import FileCoverage


def files_in(root: Path, scope: list[str]) -> list[str]:
    """The source files under the scope's paths, as git sees them (tracked or new), outside the tests."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *scope],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(line for line in out.splitlines() if line and (root / line).is_file())


def measure(root: Path, files: list[str], backend: str = "lizard", toolchain: Any = None) -> dict[str, Any]:
    functions: list[Function] = BACKENDS[backend](root, files, toolchain)
    measured = {fn.file for fn in functions}
    lines = sum(fn.nloc for fn in functions)
    for f in files:  # a file the backend does not read, or with no function: its lines that are not blank
        if f not in measured:
            lines += sum(1 for line in (root / f).read_text(errors="replace").splitlines() if line.strip())
    ccns = [fn.ccn for fn in functions]
    return {"files": len(files), "lines": lines, "functions": len(ccns),
            "ccn_max": max(ccns, default=0), "ccn_mean": round(sum(ccns) / len(ccns), 2) if ccns else 0,
            "params_max": max((fn.params for fn in functions), default=0),
            "length_max": max((fn.length for fn in functions), default=0)}


def worsened(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [f"{k} {before[k]} → {after[k]}" for k in ("ccn_max", "ccn_mean") if after.get(k, 0) > before.get(k, 0)]


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root: Path, base: str, files: list[str]) -> dict[str, set[int]]:
    """The lines of each file added or changed since base; a new file counts whole."""
    changed: dict[str, set[int]] = {}
    tracked = subprocess.run(["git", "ls-files", "--", *files], cwd=root, capture_output=True, text=True,
                             check=True).stdout.split()
    for f in files:
        if f not in tracked or subprocess.run(["git", "cat-file", "-e", f"{base}:{f}"], cwd=root,
                                              capture_output=True).returncode != 0:
            changed[f] = set(range(1, len((root / f).read_text(errors="replace").splitlines()) + 1))
            continue
        diff = subprocess.run(["git", "diff", "-U0", base, "--", f], cwd=root, capture_output=True, text=True,
                              check=True).stdout
        lines: set[int] = set()
        for line in diff.splitlines():
            m = _HUNK.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines |= set(range(start, start + count))
        if lines:
            changed[f] = lines
    return changed


def coverage_gaps(coverage: dict[str, FileCoverage], root: Path, base: str, files: list[str]) -> dict[str, list[int]]:
    """The changed lines no test ran, per file; a file the report does not know counts whole."""
    gaps = {}
    for f, lines in changed_lines(root, base, files).items():
        entry = coverage.get(f)
        hit = sorted(lines & entry.missing) if entry is not None else sorted(lines)
        if entry is None and not _executable(root / f):
            continue
        if hit:
            gaps[f] = hit
    return gaps


def _executable(path: Path) -> bool:
    """Whether a file no report names has code at all (an empty __init__.py has none)."""
    return any(line.strip() and not line.strip().startswith(("#", '"""')) for line in
               path.read_text(errors="replace").splitlines())


def totals(coverage: dict[str, FileCoverage], files: list[str]) -> dict[str, list[int]]:
    """{file: [lines executed, lines measured]} of the files the report measured."""
    return {f: [len(coverage[f].executed), coverage[f].lines] for f in files if f in coverage}


def ratio(files: dict[str, list[int]]) -> float | None:
    measured = sum(v[1] for v in files.values())
    return round(100 * sum(v[0] for v in files.values()) / measured, 2) if measured else None


def ratchet(baseline: dict[str, list[int]], unit: dict[str, list[int]], scope_files: list[str]) -> tuple[
        float | None, float | None]:
    """The project's coverage before and with the unit: the baseline with the unit's files as they are now."""
    with_unit = {f: v for f, v in baseline.items() if f not in scope_files} | unit
    return ratio(baseline), ratio(with_unit)
