"""`beck test`: the agents run the tests through beck, and the run becomes evidence.

The run writes its JUnit report under .beck/reports/ and a TestsRun event
into the store of the open iteration, with the report's hash and the git
tree of the working tree (outside .beck/) when the run ended: a verifier
that finds a different tree knows the files changed after the run.

The event's cause is $BECK_CAUSE (the event that launched the agent), so the
run joins the chain of the policy that asked for it. beck never decides
whether a phase is valid here: it records; the verifiers judge.

The agent never reads the runner's console output, which goes to the run's
log: it reads the summary of beck.junit (`ALL_OK`, or the tests that did not
pass). Without a readable report it reads NO_REPORT and the log's path; the
summary by a small model (Haiku) comes with the drivers.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from beck.events.project import Project
from beck.events.store import Event, EventStore
from beck.junit import UnreadableReport, parse, summary
from beck.toolchain import Toolchain


class TestRunError(RuntimeError):
    pass


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True,
                             env={**os.environ, **(env or {})})
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip()


def worktree_tree(root: Path) -> str | None:
    """The git tree of the working tree as it is, outside .beck/; None outside a repository."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if git(root, "add", "-A", "--", ".", ":!.beck", env=env) is None:
            return None
        return git(root, "write-tree", env=env)


def open_iteration(project: Project) -> str:
    """$BECK_ITERATION, else the latest iteration still open."""
    wanted = os.environ.get("BECK_ITERATION")
    if wanted:
        return project.find(wanted).id
    open_ = [it for it in project.iterations() if it.ended_at is None]
    if not open_:
        raise TestRunError("no open iteration: start a session first")
    return open_[-1].id


@dataclass(frozen=True)
class Run:
    event: Event
    exit_code: int
    summary: str      # what the agent reads


def run(root: Path, toolchain: Toolchain, tests: list[str], *, actor: str, unit: str | None = None,
        clause: str | None = None, store_root: Path | None = None, cause_id: str | None = None,
        iteration: str | None = None, coverage_of: list[str] | None = None) -> Run:
    """Runs in root (a unit's worktree); the reports and the event go to store_root's .beck/ (default: root).

    cause_id and iteration default to $BECK_CAUSE and the open iteration (an agent's run); beck passes them
    when a policy asks it to run tests. coverage_of: the paths to measure, when the toolchain measures.
    """
    store_root = store_root or root
    project = Project(store_root)
    store = EventStore(project.store_path(iteration or open_iteration(project)))
    try:
        cause = store.get(cause_id) if cause_id else _cause(store)
        reports = store_root / ".beck" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        name = str(uuid.uuid4())
        report, log = reports / f"{name}.xml", reports / f"{name}.log"
        coverage = reports / f"{name}.coverage"
        measured = coverage_of is not None and bool(toolchain.coverage)
        command, env = toolchain.test_argv(tests, str(report), (coverage_of or [], str(coverage)) if measured else None)
        written = root / toolchain.coverage.report if measured and toolchain.coverage.report else None
        if measured:   # coverage.py's data file, outside the tree: it would be a change of the unit
            env = {**env, "COVERAGE_FILE": str(reports / f"{name}.coverage-data")}
        if written is not None and written.is_file():
            written.unlink()   # a report of an earlier run is not this run's
        try:
            with log.open("w") as out:
                out.write("$ " + shlex.join(command) + "\n")
                out.flush()
                exit_code = subprocess.run(command, cwd=root, env={**os.environ, **env},
                                           stdout=out, stderr=subprocess.STDOUT).returncode
        except FileNotFoundError as error:
            raise TestRunError(f"cannot run {command[0]!r}: {error}") from error
        payload: dict[str, object] = {"command": shlex.join(command), "tests": tests, "exit_code": exit_code,
                                      "log": str(log.relative_to(store_root))}
        text = f"NO_REPORT (exit {exit_code}): the runner wrote no JUnit report; its output is in {payload['log']}"
        if report.is_file():
            payload["report"] = str(report.relative_to(store_root))
            payload["report_hash"] = hashlib.sha256(report.read_bytes()).hexdigest()
            try:
                parsed = parse(report, root)
                payload["counts"] = parsed.counts()
                payload["passed"] = [c.id for c in parsed.cases if c.outcome == "passed"]
                payload["not_passed"] = [c.id for c in parsed.not_passed]
                text = summary(parsed)
            except UnreadableReport:
                text = f"NO_REPORT (exit {exit_code}): the JUnit report is unreadable; the output is in {payload['log']}"
        tree = worktree_tree(root)
        if tree:
            payload["tree"] = tree
        if written is not None and written.is_file():
            shutil.copyfile(written, coverage)
        if measured and coverage.is_file():
            payload["coverage"] = str(coverage.relative_to(store_root))
            payload["coverage_format"] = toolchain.coverage.format
        payload.update({k: v for k, v in (("unit", unit), ("clause", clause)) if v})
        event = store.append("TestsRun", payload, stream=f"cycle:{unit}/{clause}" if unit and clause
                             else f"unit:{unit}" if unit else "session",
                             actor=actor, cause=cause, sha=git(root, "rev-parse", "HEAD"))
        return Run(event, exit_code, text)
    finally:
        store.close()
        project.close()


def _cause(store: EventStore) -> Event | None:
    cause = os.environ.get("BECK_CAUSE")
    if not cause:
        return None
    try:
        return store.get(cause)
    except KeyError:
        raise TestRunError(f"$BECK_CAUSE {cause!r} is not an event of this iteration") from None
