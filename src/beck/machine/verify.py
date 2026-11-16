"""The verifiers of the TDD phases: deterministic, over the evidence of a TestsRun and of git.

A verifier never runs a test: it reads the TestsRun the agent's turn caused
(its cause is the turn's CommandIssued, through $BECK_CAUSE), checks that the
working tree is still the one the run saw, and checks what changed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from beck.events.store import Event
from beck.index.anchor import is_test_file
from beck.junit import UnreadableReport, parse, summary
from beck.testrun import worktree_tree


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    problems: tuple[str, ...] = ()   # what the signature is made of
    failure: str | None = None       # the summary the agent reads, for a green to retry
    run: str | None = None           # the TestsRun's id


def changed_files(root: Path, since: str = "HEAD") -> list[str]:
    """The files that differ from `since` in the working tree, untracked ones included, outside .beck/."""
    def lines(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]
    tracked = lines("diff", "--name-only", since, "--", ".", ":!.beck")
    untracked = lines("ls-files", "--others", "--exclude-standard", "--", ".", ":!.beck")
    return sorted(set(tracked) | set(untracked))


def _the_run(root: Path, runs: list[Event], test: str) -> tuple[Event | None, Verdict | None]:
    if not runs:
        return None, Verdict(False, "no beck test run of the turn", ("no-run",))
    run = runs[-1]
    if run.payload.get("tree") != worktree_tree(root):
        return run, Verdict(False, "files changed after the last beck test run", ("stale-run",), run=run.id)
    if run.payload.get("tests") != [test]:
        return run, Verdict(False, f"the last run is not the run of the cycle's test alone ({test})",
                            ("wrong-tests",), run=run.id)
    if "report" not in run.payload:
        return run, Verdict(False, "the run wrote no readable report (the test does not compile or collect)",
                            ("no-report",), run=run.id)
    return run, None


def verify_red(root: Path, runs: list[Event], test: str, store_root: Path | None = None,
               accept_pass: bool = False) -> Verdict:
    """root: the component's worktree; store_root: where the reports are (default: root). accept_pass: a test that
    passes proves the requirement (the integration component's, over components already implemented)."""
    run, refused = _the_run(root, runs, test)
    if refused:
        return refused
    assert run is not None
    try:
        report = parse((store_root or root) / run.payload["report"], root)
    except UnreadableReport:
        return Verdict(False, "the report is unreadable", ("unreadable",), run=run.id)
    if not report.cases:
        return Verdict(False, "the test was not collected", ("not-collected",), run=run.id)
    broken = [c for c in report.cases if c.outcome == "error" and _collection_error(c.message)]
    if broken:
        return Verdict(False, "the test file does not compile or collect: fix it at once",
                       tuple(f"collect {c.message}" for c in broken), run=run.id)
    passes = bool(report.count("passed"))
    if passes and not accept_pass:
        return Verdict(False, "the test passes already: a red test must fail", ("passes",), run=run.id)
    others = [f for f in changed_files(root) if not is_test_file(Path(f))]
    if others:
        return Verdict(False, f"a red changes only tests; changed: {', '.join(others)}",
                       tuple(f"changed {f}" for f in others), run=run.id)
    return Verdict(True, "already green" if passes else "red", run=run.id)


def _collection_error(message: str) -> bool:
    lowered = message.lower()
    return any(m in lowered for m in ("collection failure", "syntaxerror", "importerror while importing",
                                       "compileerror", "syntax error"))


def verify_green(root: Path, runs: list[Event], test: str, scope: list[str],
                 store_root: Path | None = None) -> Verdict:
    """scope: the paths the component may change (its component's directories); tests may not change."""
    run, refused = _the_run(root, runs, test)
    if refused:
        return refused
    assert run is not None
    try:
        report = parse((store_root or root) / run.payload["report"], root)
    except UnreadableReport:
        return Verdict(False, "the report is unreadable", ("unreadable",), run=run.id)
    if report.not_passed or not report.count("passed"):
        return Verdict(False, "the test does not pass", tuple(f"{c.id} {c.outcome} {c.message}" for c in
                                                             report.not_passed) or ("not-passed",),
                       failure=summary(report), run=run.id)
    changed = changed_files(root)
    tests = [f for f in changed if is_test_file(Path(f))]
    if tests:
        return Verdict(False, f"a green changes no test; changed: {', '.join(tests)}",
                       tuple(f"test changed {f}" for f in tests), run=run.id)
    outside = [f for f in changed if not any(f == s or f.startswith(s.rstrip("/") + "/") for s in scope)]
    if outside:
        return Verdict(False, f"outside the component's scope: {', '.join(outside)}",
                       tuple(f"outside {f}" for f in outside), run=run.id)
    return Verdict(True, "green", run=run.id)
