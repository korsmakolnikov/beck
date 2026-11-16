"""A new project's layout, for a greenfield iteration (README, phase 1).

* Python: beck's templates (src layout, uv, pytest with Hypothesis and
  pytest-cov, ruff, mypy --strict, a Makefile task runner, GitHub Actions),
  with `{{KEY}}` replaced in paths and contents, and the toolchain wired
  (`.beck/toolchain.yml`, the coverage included);
* Elixir: `mix new`, laid beside the files already there; kent wires the
  coverage tool later (`WireCoverage`), as for any project without one.

A file already there is never overwritten; a placeholder left unresolved
is an error. Nothing is committed here: beck commits the scaffold.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path

TEMPLATES = files("beck.scaffold") / "templates"
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
LANGUAGES = ("python", "elixir")

PYTHON_TOOLCHAIN = """toolchain: 1
language: python
coverage:
  format: coverage.py
  args: ["--cov={path}", "--cov-report=json:{coverage}"]
"""


class ScaffoldError(RuntimeError):
    pass


def identifier(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]", "_", name).lower()
    return "_" + out if out[:1].isdigit() else out


def _render(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _plan(root: Path, values: dict[str, str]) -> list[tuple[str, str]]:
    out = []
    for source in sorted(p for p in Path(str(root)).rglob("*") if p.is_file()):
        dest = _render(str(source.relative_to(Path(str(root)))), values)
        text = _render(source.read_text(), values)
        left = PLACEHOLDER.findall(dest + text)
        if left:
            raise ScaffoldError(f"{dest}: unresolved placeholders {sorted(set(left))}")
        out.append((dest, text))
    return out


def python(project: Path, name: str, python_version: str = "3.12") -> list[str]:
    values = {"PROJECT_NAME": name, "PACKAGE": identifier(name), "PYTHON": python_version,
              "PYTHON_TAG": "py" + python_version.replace(".", ""), "CI_BADGE": ""}
    scheduled = _plan(Path(str(TEMPLATES / "python")), values)
    clash = [d for d, _ in scheduled if (project / d).exists()]
    if clash:
        raise ScaffoldError(f"the scaffold would overwrite {', '.join(clash)}")
    for dest, text in scheduled:
        (project / dest).parent.mkdir(parents=True, exist_ok=True)
        (project / dest).write_text(text)
    toolchain = project / ".beck" / "toolchain.yml"
    if not toolchain.exists():
        toolchain.parent.mkdir(exist_ok=True)
        toolchain.write_text(PYTHON_TOOLCHAIN)
    return [d for d, _ in scheduled]


def elixir(project: Path, name: str) -> list[str]:
    app = identifier(name)
    if shutil.which("mix") is None:
        raise ScaffoldError("mix is not installed: Elixir is needed to lay an Elixir project")
    with tempfile.TemporaryDirectory() as tmp:
        done = subprocess.run(["mix", "new", str(Path(tmp) / app), "--app", app], capture_output=True, text=True)
        if done.returncode != 0:
            raise ScaffoldError(f"mix new failed: {done.stderr or done.stdout}")
        made = Path(tmp) / app
        scheduled = sorted(str(p.relative_to(made)) for p in made.rglob("*") if p.is_file())
        clash = [d for d in scheduled if (project / d).exists()]
        if clash:
            raise ScaffoldError(f"the scaffold would overwrite {', '.join(clash)}")
        for dest in scheduled:
            (project / dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(made / dest, project / dest)
    return scheduled


def scaffold(project: Path, language: str, name: str) -> list[str]:
    if language not in LANGUAGES:
        raise ScaffoldError(f"beck lays {', '.join(LANGUAGES)} projects, not {language!r}")
    return python(project, name) if language == "python" else elixir(project, name)
