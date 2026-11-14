"""The measures behind their interfaces: complexity backends, coverage formats, the ratchet, the toolchain's choice."""

import json

import pytest

from beck.measures import measure, ratchet, ratio
from beck.measures.complexity import by_elixir, by_lizard
from beck.measures.coverage import READERS
from beck.toolchain import Coverage, ToolchainError, load

ELIXIR = '''defmodule A do
  def f(x, y) when x > 0 do
    case x do
      1 -> :a
      _ -> if y && x, do: :b, else: :c
    end
  end

  defp g(x), do: x

  def h(x) do
    with {:ok, a} <- x, {:ok, b} <- a do
      b
    else
      _ -> nil
    end
  end
end
'''


def test_elixir_complexity_counts_the_decisions_of_each_function(tmp_path):
    (tmp_path / "a.ex").write_text(ELIXIR)
    fns = {f.name: f for f in by_elixir(tmp_path, ["a.ex"])}
    assert set(fns) == {"f/2", "g/1", "h/1"}
    assert fns["f/2"].ccn == 1 + 1 + 1 + 1 + 1     # the guard, case's second clause, if, &&
    assert fns["g/1"].ccn == 1 and fns["g/1"].params == 1
    assert fns["h/1"].ccn == 1 + 2                  # two <-; one else clause adds nothing
    assert fns["f/2"].line == 2 and fns["f/2"].length == 6


def test_measure_uses_the_backend_and_counts_the_rest_by_lines(tmp_path):
    (tmp_path / "a.ex").write_text(ELIXIR)
    (tmp_path / "b.py").write_text("def f(a, b):\n    if a:\n        return b\n    return a\n")
    m = measure(tmp_path, ["a.ex", "b.py"], "elixir")
    assert m["functions"] == 3 and m["ccn_max"] == 5
    assert m["lines"] > 3                           # b.py, which elixir's backend does not read, by its lines
    assert measure(tmp_path, ["b.py"], "lizard")["params_max"] == 2
    assert by_lizard(tmp_path, ["a.ex"]) == []


def test_the_coverage_formats_read_into_one_shape(tmp_path):
    (tmp_path / "py.json").write_text(json.dumps({"files": {str(tmp_path / "src/a.py"): {
        "executed_lines": [1, 2], "missing_lines": [3]}}}))
    (tmp_path / "ex.json").write_text(json.dumps({"source_files": [{"name": "lib/a.ex",
                                                                     "coverage": [None, 1, 0, 3]}]}))
    (tmp_path / "c.info").write_text("TN:\nSF:src/a.c\nDA:1,1\nDA:2,0\nDA:3,4\nend_of_record\n")
    py = READERS["coverage.py"](tmp_path / "py.json", tmp_path)["src/a.py"]
    ex = READERS["excoveralls"](tmp_path / "ex.json", tmp_path)["lib/a.ex"]
    c = READERS["lcov"](tmp_path / "c.info", tmp_path)["src/a.c"]
    assert (py.executed, py.missing) == ({1, 2}, {3})
    assert (ex.executed, ex.missing) == ({2, 4}, {3})
    assert (c.executed, c.missing) == ({1, 3}, {2})


def test_the_ratchet_puts_the_unit_s_files_in_the_project_s_coverage():
    baseline = {"a.py": [8, 10], "b.py": [10, 10]}
    assert ratio(baseline) == 90.0
    assert ratchet(baseline, {"a.py": [10, 10]}, ["a.py"]) == (90.0, 100.0)
    assert ratchet(baseline, {"c.py": [0, 10]}, ["c.py"]) == (90.0, 60.0)
    assert ratchet({}, {"c.py": [0, 10]}, ["c.py"]) == (None, 0.0)


def test_the_toolchain_names_the_coverage_and_the_complexity(tmp_path):
    (tmp_path / ".beck").mkdir()
    path = tmp_path / ".beck/toolchain.yml"
    path.write_text("toolchain: 1\nlanguage: elixir\ncoverage:\n  format: excoveralls\n"
                    "  command: [mix, coveralls.json, '{tests}']\n  report: cover/excoveralls.json\n")
    t = load(tmp_path)
    assert t.coverage == Coverage("excoveralls", [], ["mix", "coveralls.json", "{tests}"], "cover/excoveralls.json")
    assert t.complexity == "credo"
    argv, _ = t.test_argv(["test/a_test.exs:3"], "r.xml", (["lib"], "c.json"))
    assert argv == ["mix", "coveralls.json", "test/a_test.exs:3"]
    path.write_text("toolchain: 1\nlanguage: python\ncoverage: ['--cov={path}']\nmeasures: {complexity: lizard}\n")
    t = load(tmp_path)
    assert t.test_argv([], "r.xml", (["src/a", "src/b"], "c.json"))[0][-2:] == ["--cov=src/a", "--cov=src/b"]
    path.write_text("toolchain: 1\nlanguage: python\nmeasures: {complexity: radon}\n")
    with pytest.raises(ToolchainError, match="complexity"):
        load(tmp_path)


CREDO_OUT = '''Compiling 1 file (.ex)
{"issues": [
 {"check": "Credo.Check.Refactor.CyclomaticComplexity", "filename": "a.ex", "line_no": 2,
  "message": "Function is too complex (cyclomatic complexity is 5, max is 0).", "trigger": "f"},
 {"check": "Credo.Check.Refactor.CyclomaticComplexity", "filename": "a.ex", "line_no": 11,
  "message": "Function is too complex (cyclomatic complexity is 1, max is 0).", "trigger": "h"}]}
'''


def fake_mix(tmp_path, monkeypatch, body):
    import stat
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    mix = bin_ / "mix"
    mix.write_text(f"#!/bin/sh\ntest -f .beck/credo-complexity.exs || exit 9\n{body}\n")
    mix.chmod(mix.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}:{__import__('os').environ['PATH']}")


def test_credo_gives_the_complexity_and_tree_sitter_the_rest(tmp_path, monkeypatch):
    from beck.measures.complexity import by_credo
    (tmp_path / "a.ex").write_text(ELIXIR)
    fake_mix(tmp_path, monkeypatch, f"cat <<'JSON'\n{CREDO_OUT}JSON\nexit 2")
    fns = {f.name: f for f in by_credo(tmp_path, ["a.ex"])}
    assert fns["f/2"].ccn == 5 and fns["h/1"].ccn == 1      # Credo's: it does not count with's clauses
    assert fns["g/1"].ccn == 1 and fns["g/1"].params == 1    # not reported: tree-sitter's
    assert not (tmp_path / ".beck/credo-complexity.exs").exists()


def test_without_credo_tree_sitter_stands_in(tmp_path, monkeypatch):
    from beck.measures.complexity import by_credo
    (tmp_path / "a.ex").write_text(ELIXIR)
    fake_mix(tmp_path, monkeypatch, "echo '** (Mix) The task \"credo\" could not be found' >&2\nexit 1")
    assert {f.name: f.ccn for f in by_credo(tmp_path, ["a.ex"])} == {f.name: f.ccn for f in by_elixir(tmp_path, ["a.ex"])}
