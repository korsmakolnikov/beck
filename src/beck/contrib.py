"""argyris's changes to beck, as pull requests to beck's repository (README, "Actors").

beck is an open-source application: nothing of it is installed as files to
edit in place. argyris changes beck's agents (their prompts, their schemas,
smart_audit's rules) in a worktree of beck's source of its own, one per
change, and proposes each as a pull request, opened with `gh`:

    beck contrib start <slug>     a worktree on argyris/<slug>, from the base branch of the remote
    beck contrib diff <slug>      what the worktree changes, for the user's approval
    beck contrib submit <slug> --title "fix(beck): …" < body
                                  commit, push, open the pull request; prints its URL

The source is the git checkout beck runs from; without one (beck installed
from a package), a clone of `contrib.repo` in beck's cache, made with `gh`.
The worktrees live in the cache too, never in the checkout beck runs from.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import beck

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TITLE = re.compile(r"^(fix|feat|docs|refactor|test|perf|chore)(\([a-z0-9-]+\))?!?: \S.*$")


class ContribError(RuntimeError):
    pass


def cache() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "beck"


def _run(argv: list[str], cwd: Path) -> str:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise ContribError(f"cannot run {argv[0]!r}: {error}") from error
    if done.returncode != 0:
        raise ContribError(f"`{' '.join(argv[:4])}…` failed: {(done.stderr or done.stdout).strip()[-1000:]}")
    return done.stdout.strip()


def gh() -> str:
    return os.environ.get("BECK_GH", "gh")


def source(config: dict[str, Any]) -> Path:
    """beck's source: the checkout it runs from, else a clone of contrib.repo in the cache."""
    here = Path(beck.__file__).resolve().parent
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here, capture_output=True, text=True)
    if top.returncode == 0:
        return Path(top.stdout.strip())
    clone = cache() / "source"
    if not clone.is_dir():
        repo = config["contrib"].get("repo")
        if not repo:
            raise ContribError("beck runs from no git checkout: set `contrib.repo` (owner/name) in the config")
        clone.parent.mkdir(parents=True, exist_ok=True)
        _run([gh(), "repo", "clone", repo, str(clone)], clone.parent)
    return clone


def worktree(slug: str) -> Path:
    if not SLUG.match(slug):
        raise ContribError(f"{slug!r} is not a slug: lower-case words joined by -")
    return cache() / "contrib" / slug


def start(slug: str, config: dict[str, Any]) -> Path:
    path = worktree(slug)
    if path.is_dir():
        return path
    src, base = source(config), config["contrib"]["base"]
    _run(["git", "fetch", "-q", "origin", base], src)
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-q", "-b", f"argyris/{slug}", str(path), f"origin/{base}"], src)
    return path


def diff(slug: str) -> str:
    path = worktree(slug)
    if not path.is_dir():
        raise ContribError(f"no change {slug!r}: `beck contrib start {slug}` first")
    _run(["git", "add", "-A", "-N"], path)   # new files show in the diff
    return _run(["git", "diff", "--stat", "-p"], path)


def submit(slug: str, title: str, body: str, config: dict[str, Any]) -> str:
    """Commits the worktree's changes, pushes them, opens the pull request; its URL."""
    path = worktree(slug)
    if not path.is_dir():
        raise ContribError(f"no change {slug!r}: `beck contrib start {slug}` first")
    if not TITLE.match(title):
        raise ContribError(f"{title!r} is not a Conventional Commit subject, as `fix(beck): what the agents now do`")
    if not body.strip():
        raise ContribError("the pull request needs a body: the violations it settles and why the change")
    _run(["git", "add", "-A"], path)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path).returncode == 0:
        raise ContribError(f"{slug!r} changes nothing")
    _run(["git", "commit", "-q", "-m", title, "-m", body.strip()], path)
    branch = f"argyris/{slug}"
    _run(["git", "push", "-q", "-u", "origin", branch], path)
    out = _run([gh(), "pr", "create", "--base", config["contrib"]["base"], "--head", branch, "--title", title,
                "--body", body.strip()], path)
    return out.splitlines()[-1] if out else ""
