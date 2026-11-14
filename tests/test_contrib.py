"""argyris's changes to beck: a worktree per change, then commit, push and a pull request with gh."""

import io
import stat
import subprocess

import pytest

from beck import config as beck_config
from beck import contrib
from beck.cli import main


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """beck's source: a checkout whose origin is a bare repository; gh is a script that records its call."""
    bare = tmp_path / "beck.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "master", str(bare))
    src = tmp_path / "beck"
    git(tmp_path, "clone", "-q", str(bare), str(src))
    for k, v in (("user.email", "a@example.com"), ("user.name", "a")):
        git(src, "config", k, v)
    (src / "prompt.md").write_text("Read only your group.\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "start")
    git(src, "push", "-q", "origin", "HEAD:master")
    fake = tmp_path / "gh"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/gh-args\n"
                    "echo https://github.com/owner/beck/pull/7\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BECK_GH", str(fake))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(contrib, "source", lambda config: src)
    return tmp_path, bare


def test_a_change_is_a_worktree_then_a_pushed_branch_and_a_pull_request(upstream):
    tmp, bare = upstream
    config = beck_config.load(tmp)
    path = contrib.start("scope-rule", config)
    assert path == tmp / "cache/beck/contrib/scope-rule" and contrib.start("scope-rule", config) == path
    (path / "prompt.md").write_text("Read only the files of your group.\n")
    assert "+Read only the files of your group." in contrib.diff("scope-rule")
    url = contrib.submit("scope-rule", "fix(beck): unclebob reads only its group's files", "Settles V1.", config)
    assert url == "https://github.com/owner/beck/pull/7"
    assert git(bare, "log", "-1", "--format=%s", "argyris/scope-rule") == "fix(beck): unclebob reads only its group's files"
    args = (tmp / "gh-args").read_text().splitlines()
    assert args[:2] == ["pr", "create"] and "argyris/scope-rule" in args and "Settles V1." in args


def test_a_submit_needs_a_conventional_title_a_body_and_a_change(upstream):
    tmp, _ = upstream
    config = beck_config.load(tmp)
    contrib.start("nothing", config)
    with pytest.raises(contrib.ContribError, match="Conventional Commit"):
        contrib.submit("nothing", "Fix the prompt", "why", config)
    with pytest.raises(contrib.ContribError, match="body"):
        contrib.submit("nothing", "fix(beck): x", " ", config)
    with pytest.raises(contrib.ContribError, match="changes nothing"):
        contrib.submit("nothing", "fix(beck): x", "why", config)
    with pytest.raises(contrib.ContribError, match="slug"):
        contrib.start("Not A Slug", config)


def test_the_cli_starts_and_submits(upstream, capsys, monkeypatch):
    tmp, _ = upstream
    assert main(["contrib", "start", "cli-change", "--project", str(tmp)]) == 0
    path = capsys.readouterr().out.strip()
    (tmp / "cache/beck/contrib/cli-change/prompt.md").write_text("changed\n")
    assert path.endswith("contrib/cli-change")
    monkeypatch.setattr("sys.stdin", io.StringIO("Settles V2."))
    assert main(["contrib", "submit", "cli-change", "--title", "fix(beck): y", "--project", str(tmp)]) == 0
    assert capsys.readouterr().out.strip() == "https://github.com/owner/beck/pull/7"
    assert main(["contrib", "diff", "absent", "--project", str(tmp)]) == 1
