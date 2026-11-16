"""How to run a project's tests: .beck/toolchain.yml.

    toolchain: 1
    language: python                 # python | elixir: picks the defaults
    exec: [docker, compose, exec, app]   # optional prefix of every command
    env: {MIX_ENV: test}             # optional extra environment
    test: [uv, run, pytest, "--junitxml={report}", "{tests}"]   # optional
    build: [uv, run, python, -m, compileall, -q, src]   # optional: does the code compile
    deps: [mix, deps.get]            # optional: installs the dependencies (python: none, uv run syncs)
    timeout: 300                     # optional: seconds a test run may take before beck stops it
    coverage:                        # optional: how to measure the coverage
      format: coverage.py            # coverage.py | excoveralls | lcov: how beck reads the report
      args: ["--cov={path}", "--cov-report=json:{coverage}"]   # added to the test command
      command: [mix, coveralls.json, "{tests}"]   # or: the test command to run in its place
      report: cover/excoveralls.json # when the tool picks the report's path (relative to the tree)
    measures:
      complexity: lizard             # lizard | credo (tree-sitter as its fallback) | elixir (tree-sitter only)
    check:                           # optional: the static checks
      - name: ruff
        command: [uv, run, ruff, check, --output-format, json, "{paths}"]
        format: ruff                 # ruff | mypy | credo | text: how beck reads the output
        scope: paths                 # paths (per unit) | project (only with beck check --all)

A list as `coverage` is its `args`, with the coverage.py format. In the
coverage's args and command, "{path}" expands to one argument per path
measured and "{coverage}" to the report's path beck reads.

In a check's command, "{paths}" expands to the paths checked. In `test`,
"{tests}" expands to the selected tests (one argument each, none when no
test is selected) and "{report}" to the path of the JUnit report the
run must write; the same path is in the environment as BECK_REPORT. Without
the file, the language comes from mix.exs or pyproject.toml.

Elixir writes no JUnit report on its own: the project needs the
junit_formatter package, configured to write to $BECK_REPORT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS = {
    "python": ["uv", "run", "pytest", "--junitxml={report}", "{tests}"],
    "elixir": ["mix", "test", "{tests}"],
}

# Python writes no bytecode: a .pyc with the same mtime and size as a rewritten file hides the rewrite
DEFAULT_ENV = {"python": {"PYTHONDONTWRITEBYTECODE": "1"}, "elixir": {}}

FORMATS = ("ruff", "mypy", "credo", "text")
COVERAGE_FORMATS = ("coverage.py", "excoveralls", "lcov")
COMPLEXITY = ("lizard", "credo", "elixir")
DEFAULT_COMPLEXITY = {"python": "lizard", "elixir": "credo"}
SCOPES = ("paths", "project")


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    format: str = "text"
    scope: str = "paths"


DEFAULT_DEPS: dict[str, list[str]] = {"python": [], "elixir": ["mix", "deps.get"]}

DEFAULT_BUILD = {"python": ["python3", "-m", "compileall", "-q", "."], "elixir": ["mix", "compile"]}

DEFAULT_CHECKS = {
    "python": [Check("ruff", ["uv", "run", "ruff", "check", "--output-format", "json", "{paths}"], "ruff"),
               Check("mypy", ["uv", "run", "mypy", "--output", "json", "{paths}"], "mypy")],
    "elixir": [Check("credo", ["mix", "credo", "--strict", "--format", "json", "{paths}"], "credo"),
               Check("dialyzer", ["mix", "dialyzer"], "text", "project")],
}


class ToolchainError(ValueError):
    pass


@dataclass(frozen=True)
class Coverage:
    format: str = "coverage.py"
    args: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    report: str | None = None

    def __bool__(self) -> bool:
        return bool(self.args or self.command)


@dataclass(frozen=True)
class Toolchain:
    language: str
    test: list[str]
    exec: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    check: list[Check] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    build: list[str] = field(default_factory=list)
    complexity: str = "lizard"
    deps: list[str] = field(default_factory=list)
    timeout: float = 300                # a test run that takes longer is stopped: a test that hangs

    @staticmethod
    def _expand(parts: list[str], paths: list[str], report: str) -> list[str]:
        out: list[str] = []
        for part in parts:
            if "{path}" in part:
                out += [part.replace("{path}", p) for p in paths]
            else:
                out.append(part.replace("{coverage}", report))
        return out

    def test_argv(self, tests: list[str], report: str, coverage: tuple[list[str], str] | None = None
                  ) -> tuple[list[str], dict[str, str]]:
        """The test command for these tests, and its extra environment; coverage: (the paths, the report)."""
        template = self.test
        if coverage is not None and self.coverage.command:
            template = self._expand(self.coverage.command, *coverage)
        elif coverage is not None:
            template = [*self.test, *self._expand(self.coverage.args, *coverage)]
        command = [p.replace("{report}", report) for p in template]
        return self.argv(command, tests, placeholder="{tests}", env={"BECK_REPORT": report})

    def argv(self, template: list[str], values: list[str], *, placeholder: str = "{paths}",
             env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
        """template with the placeholder expanded to values, wrapped in exec and env."""
        command: list[str] = []
        for part in template:
            command += values if part == placeholder else [part]
        full_env = {**self.env, **(env or {})}
        if self.exec:
            pairs = [f"{k}={v}" for k, v in full_env.items()]
            return [*self.exec, *(["env", *pairs] if pairs else []), *command], {}
        return command, full_env

    def source_roots(self, root: Path) -> list[str]:
        """Where the project's code is: what the coverage of the whole suite measures."""
        if self.language == "python":
            return ["src"] if (root / "src").is_dir() else ["."]
        return ["lib"]

    def component_paths(self, root: Path, namespace: list[str]) -> list[str]:
        """The directories (and the Elixir facade file, the Python module) of a component, as they exist."""
        if self.language == "python":
            base = "src" if (root / "src").is_dir() else "."
            candidates = [str(Path(base, *namespace)), str(Path(base, *namespace[:-1], namespace[-1] + ".py"))]
        else:
            candidates = [str(Path("lib", *namespace)), str(Path("lib", *namespace[:-1], namespace[-1] + ".ex"))]
        return [c for c in candidates if (root / c).exists()]

    def selector(self, file: str, name: str, line: int) -> str:
        """How the runner names one test of the index."""
        if self.language == "python":
            return f"{file}::{name.replace('.', '::')}"
        return f"{file}:{line}"


# the body kent gives every interface ("no logic: every body raises not implemented"), per language
STUBS = {"python": ("raise NotImplementedError", ".py"), "elixir": ('raise "not implemented"', ".ex")}


def load(root: Path) -> Toolchain:
    path = root / ".beck" / "toolchain.yml"
    if not path.is_file():
        language = detect(root)
        return Toolchain(language, DEFAULTS[language], env=dict(DEFAULT_ENV[language]), check=DEFAULT_CHECKS[language],
                         build=DEFAULT_BUILD[language], complexity=DEFAULT_COMPLEXITY[language],
                         deps=DEFAULT_DEPS[language])
    config: Any = yaml.safe_load(path.read_text()) or {}
    if not isinstance(config, dict) or config.get("toolchain") != 1:
        raise ToolchainError(f"{path}: `toolchain: 1` is required")
    language = str(config.get("language"))
    if language not in DEFAULTS:
        raise ToolchainError(f"{path}: `language` must be one of {', '.join(DEFAULTS)}")
    for key, kind in (("exec", list), ("env", dict), ("test", list), ("check", list), ("build", list),
                      ("measures", dict), ("deps", list)):
        if key in config and not isinstance(config[key], kind):
            raise ToolchainError(f"{path}: `{key}` must be a {kind.__name__}")
    checks = DEFAULT_CHECKS[language]
    if "check" in config:
        checks = [_check(path, c) for c in config["check"]]
    return Toolchain(language, [str(p) for p in config.get("test", DEFAULTS[language])],
                     [str(p) for p in config.get("exec", [])],
                     {**DEFAULT_ENV[language], **{str(k): str(v) for k, v in (config.get("env") or {}).items()}},
                     checks,
                     _coverage(path, config.get("coverage")),
                     [str(p) for p in config.get("build", DEFAULT_BUILD[language])],
                     _complexity(path, (config.get("measures") or {}).get("complexity", DEFAULT_COMPLEXITY[language])),
                     [str(p) for p in config.get("deps", DEFAULT_DEPS[language])],
                     _timeout(path, config.get("timeout", 300)))


def _timeout(path: Path, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ToolchainError(f"{path}: `timeout` must be a number of seconds")
    return float(value)


def _coverage(path: Path, c: Any) -> Coverage:
    if c is None:
        return Coverage()
    if isinstance(c, list):
        return Coverage(args=[str(p) for p in c])
    if not isinstance(c, dict):
        raise ToolchainError(f"{path}: `coverage` must be a list of arguments or a mapping")
    coverage = Coverage(str(c.get("format", "coverage.py")), [str(p) for p in c.get("args", [])],
                        [str(p) for p in c.get("command", [])], str(c["report"]) if c.get("report") else None)
    if coverage.format not in COVERAGE_FORMATS:
        raise ToolchainError(f"{path}: coverage: `format` must be one of {', '.join(COVERAGE_FORMATS)}")
    if coverage.args and coverage.command:
        raise ToolchainError(f"{path}: coverage: `args` or `command`, not both")
    return coverage


def _complexity(path: Path, name: Any) -> str:
    if name not in COMPLEXITY:
        raise ToolchainError(f"{path}: measures: `complexity` must be one of {', '.join(COMPLEXITY)}")
    return str(name)


def _check(path: Path, c: Any) -> Check:
    if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not isinstance(c.get("command"), list):
        raise ToolchainError(f"{path}: every check needs a `name` and a `command` list")
    check = Check(c["name"], [str(p) for p in c["command"]], c.get("format", "text"), c.get("scope", "paths"))
    if check.format not in FORMATS:
        raise ToolchainError(f"{path}: check {check.name}: `format` must be one of {', '.join(FORMATS)}")
    if check.scope not in SCOPES:
        raise ToolchainError(f"{path}: check {check.name}: `scope` must be one of {', '.join(SCOPES)}")
    return check


def detect(root: Path) -> str:
    if (root / "mix.exs").is_file():
        return "elixir"
    if (root / "pyproject.toml").is_file():
        return "python"
    raise ToolchainError(f"no .beck/toolchain.yml and no mix.exs or pyproject.toml in {root}")
