"""`beck check`: the static checks of .beck/toolchain.yml, run by beck, read deterministically.

Like `beck test`, the agent reads only `ALL_OK` or one line per finding
(`file:line:col code message`); every check's console output goes to its log
under .beck/reports/. A check whose output beck cannot parse reports
NO_PARSE and its log (the summary by Haiku comes with the drivers). The run
is recorded as a ChecksRun event.

A check has a scope: `paths` checks (a linter, a type checker on files) run
on the paths asked, a unit's directory by default; `project` checks
(dialyzer) run only with --all, since they cost too much to run per unit.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from beck.events.project import Project
from beck.events.store import Event, EventStore
from beck.testrun import _cause, git, open_iteration, worktree_tree
from beck.toolchain import Check, Toolchain


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    column: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.column} {self.code} {self.message}"


class NoParse(ValueError):
    pass


def _ruff(output: str, root: Path) -> list[Finding]:
    return [Finding(_rel(d["filename"], root), d["location"]["row"], d["location"]["column"],
                    d.get("code") or "?", d["message"]) for d in json.loads(output or "[]")]


def _mypy(output: str, root: Path) -> list[Finding]:
    found = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        if d.get("severity") == "note":
            continue
        found.append(Finding(_rel(d["file"], root), d["line"], d.get("column", 0) + 1, d.get("code") or "error",
                             d["message"]))
    return found


def _credo(output: str, root: Path) -> list[Finding]:
    start = output.find("{")
    if start < 0:
        raise NoParse("no JSON in credo's output")
    issues = json.loads(output[start:])["issues"]
    return [Finding(_rel(i["filename"], root), i.get("line_no") or 0, i.get("column") or 0,
                    i.get("check", "?").removeprefix("Elixir.Credo.Check."), i["message"]) for i in issues]


_TEXT = re.compile(r"^([\w./-]+\.\w+):(\d+)(?::(\d+))?:?\s*(.*)$")


def _text(output: str, root: Path) -> list[Finding]:
    """`file:line[:col]: message`, the common shape of compilers and linters."""
    found = []
    for line in output.splitlines():
        m = _TEXT.match(line.strip())
        if m and (root / m.group(1)).is_file():
            found.append(Finding(m.group(1), int(m.group(2)), int(m.group(3) or 0), "-", m.group(4)))
    if not found:
        raise NoParse("no file:line: finding in the output")
    return found


PARSERS = {"ruff": _ruff, "mypy": _mypy, "credo": _credo, "text": _text}


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    return str(p.relative_to(root.resolve())) if p.is_absolute() and p.is_relative_to(root.resolve()) else path


@dataclass(frozen=True)
class Result:
    check: str
    exit_code: int
    log: str
    findings: list[Finding] | None   # None: the output could not be parsed

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.findings


def run_check(root: Path, toolchain: Toolchain, check: Check, paths: list[str], log: Path,
              store_root: Path | None = None) -> Result:
    command, env = toolchain.argv(check.command, paths)
    with log.open("w") as out:
        out.write("$ " + shlex.join(command) + "\n")
        out.flush()
        try:
            proc = subprocess.run(command, cwd=root, env={**os.environ, **env},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as error:
            out.write(str(error) + "\n")
            return Result(check.name, 127, str(log.relative_to(store_root or root)), None)
        out.write(proc.stdout)
        if proc.stderr:
            out.write("--- stderr ---\n" + proc.stderr)
    rel = str(log.relative_to(store_root or root))
    try:
        findings = PARSERS[check.format](proc.stdout, root)
    except (NoParse, json.JSONDecodeError, KeyError, TypeError):
        return Result(check.name, proc.returncode, rel, [] if proc.returncode == 0 else None)
    return Result(check.name, proc.returncode, rel, findings)


def summary(results: list[Result]) -> str:
    if all(r.ok for r in results):
        return f"ALL_OK ({', '.join(r.check for r in results) or 'no check'})"
    lines = []
    for r in results:
        if r.ok:
            continue
        if r.findings is None:
            lines.append(f"NO_PARSE {r.check} (exit {r.exit_code}): its output is in {r.log}")
        elif not r.findings:
            lines.append(f"NOT_OK {r.check} (exit {r.exit_code}, no finding parsed): its output is in {r.log}")
        else:
            lines.append(f"NOT_OK {r.check} ({len(r.findings)} findings)")
            lines.extend(f"- {f}" for f in r.findings)
    return "\n".join(lines)


@dataclass(frozen=True)
class ChecksRun:
    event: Event
    results: list[Result]
    summary: str

    @property
    def exit_code(self) -> int:
        return 0 if all(r.ok for r in self.results) else 1


def run(root: Path, toolchain: Toolchain, paths: list[str], *, whole_project: bool, actor: str,
        unit: str | None = None, store_root: Path | None = None, cause_id: str | None = None,
        iteration: str | None = None) -> ChecksRun:
    """Runs in root (a unit's worktree); the logs and the event go to store_root's .beck/ (default: root)."""
    store_root = store_root or root
    checks = [c for c in toolchain.check if whole_project or c.scope == "paths"]
    project = Project(store_root)
    store = EventStore(project.store_path(iteration or open_iteration(project)))
    try:
        cause = store.get(cause_id) if cause_id else _cause(store)
        reports = store_root / ".beck" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        name = str(uuid.uuid4())
        results = [run_check(root, toolchain, c, paths if c.scope == "paths" else [],
                             reports / f"{name}.{c.name}.log", store_root) for c in checks]
        payload: dict[str, object] = {
            "paths": paths, "whole_project": whole_project,
            "checks": [{"name": r.check, "exit_code": r.exit_code, "log": r.log,
                        "findings": None if r.findings is None else len(r.findings)} for r in results],
        }
        tree = worktree_tree(root)
        if tree:
            payload["tree"] = tree
        if unit:
            payload["unit"] = unit
        event = store.append("ChecksRun", payload, stream=f"unit:{unit}" if unit else "session", actor=actor,
                             cause=cause, sha=git(root, "rev-parse", "HEAD"))
        return ChecksRun(event, results, summary(results))
    finally:
        store.close()
        project.close()
