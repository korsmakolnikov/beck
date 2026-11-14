"""The fitness generator judges the architecture of every unit, so it is tested."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from beck.cli import main
from beck.fitness.generate import Case, camelize, compile_support, detect_stack, expand, write

EXAMPLE = Path(__file__).parents[1] / "docs" / "examples" / "bookmarks"


@pytest.fixture
def arch():
    return yaml.safe_load((EXAMPLE / "architecture.yaml").read_text())


def by_id(cases):
    return {c.id: c for c in cases}


def test_every_element_gets_an_existence_case(arch):
    cases = by_id(expand(arch).cases)
    for id_ in ("tagging", "bookmark-tagger", "tag-store", "archive", "archiver", "storage"):
        assert cases[f"{id_}-is-defined"] == Case(f"{id_}-is-defined", "defined", None, (id_,))


def test_every_component_gets_its_declared_dependencies(arch):
    cases = by_id(expand(arch).cases)
    assert cases["tag-store-declared-dependencies"] == Case(
        "tag-store-declared-dependencies", "only_depends_on", "tag-store", ("storage",))
    tagger = arch["elements"][0]["elements"][0]
    tagger.pop("depends_on")
    assert by_id(expand(arch).cases)["bookmark-tagger-declared-dependencies"].targets == ()


def test_a_foreach_expands_over_its_scope_without_the_exceptions(arch):
    ids = [c.id for c in expand(arch).cases if c.id.startswith("tagging-components")]
    assert ids == ["tagging-components-reach-storage-only-through-tag-store[bookmark-tagger]"]
    arch["assertions"][2]["foreach"].pop("within")
    arch["assertions"][2]["foreach"]["except"] = []
    cases = [c for c in expand(arch).cases if c.id.startswith("tagging-components")]
    assert [(c.origin, c.targets) for c in cases] == [
        ("bookmark-tagger", ("storage",)), ("tag-store", ("storage",)), ("archiver", ("storage",))]


def test_nested_foreach_binds_both_variables(arch):
    arch["assertions"] = [{"id": "independent", "foreach": {
        "over": "component", "as": "a", "assert": {"foreach": {
            "over": "component", "within": "archive", "as": "b",
            "assert": {"no_dependency": {"from": "$b", "to": "$a"}}}}}}]
    cases = [c for c in expand(arch).cases if c.id.startswith("independent")]
    assert [c.id for c in cases][:2] == ["independent[bookmark-tagger][archiver]", "independent[tag-store][archiver]"]
    assert cases[0].origin == "archiver" and cases[0].targets == ("bookmark-tagger",)


def test_contained_in_allows_the_containers_and_counts_libraries_as_leaves(arch):
    case = by_id(expand(arch).cases)["classes-only-in-components"]
    assert set(case.targets) == {"bookmark-tagger", "tag-store", "archiver", "storage"}
    assert case.allow == ("bookmarks", "tagging", "archive")


def test_a_rule_is_left_to_kent(arch):
    x = expand(arch)
    assert x.rules == {"tagging-is-auditable": "Every write of a tagging component is recorded in the audit log."}
    assert "tagging-is-auditable" not in by_id(x.cases)


def test_an_assertion_id_that_clashes_with_a_generated_one_is_refused(arch):
    arch["assertions"][0]["id"] = "tag-store-is-defined"
    with pytest.raises(ValueError, match="tag-store-is-defined"):
        expand(arch)


def test_camelize():
    assert camelize("bookmark_tagger") == "BookmarkTagger"
    assert camelize("bookmarks") == "Bookmarks"


def test_the_stack_comes_from_the_project(tmp_path):
    with pytest.raises(ValueError, match="unsupported stack"):
        detect_stack(tmp_path)
    (tmp_path / "pyproject.toml").touch()
    assert detect_stack(tmp_path) == "python"
    (tmp_path / "mix.exs").touch()
    assert detect_stack(tmp_path) == "elixir"


MODULES = {
    "bookmarks/tagging/bookmark_tagger/core.py": "from bookmarks.tagging.tag_store import x\n",
    "bookmarks/tagging/tag_store/core.py": "from bookmarks.storage import db\nimport json\n",
    "bookmarks/archive/archiver/core.py": "from bookmarks.tagging.tag_store import y\n",
    "bookmarks/storage/db.py": "",
}


@pytest.fixture
def python_project(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    for path, text in MODULES.items():
        file = tmp_path / "src" / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        for parent in file.relative_to(tmp_path / "src").parents:
            if str(parent) != ".":
                (tmp_path / "src" / parent / "__init__.py").touch()
    return tmp_path


def run_pytest(root):
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                         cwd=root, capture_output=True, text=True)
    return out.returncode, out.stdout


def test_the_python_fitness_functions_pass_and_fail(python_project, arch):
    write(python_project, arch)
    code, out = run_pytest(python_project)
    assert code == 0, out
    assert "12 passed" in out
    tagger = python_project / "src/bookmarks/tagging/bookmark_tagger/core.py"
    tagger.write_text(tagger.read_text() + "from bookmarks.storage import db\n")
    code, out = run_pytest(python_project)
    assert code == 1
    # pytest keeps the whole failure message on the summary line under CI (the CI variable): keep the test id only
    ids = [line.removeprefix("FAILED ").split(" - ", 1)[0] for line in out.splitlines() if line.startswith("FAILED")]
    failed = sorted(i.split("[", 1)[1][:-1] for i in ids)
    assert failed == ["bookmark-tagger-declared-dependencies",
                      "tagging-components-reach-storage-only-through-tag-store[bookmark-tagger]"]


def test_the_elixir_table_names_modules(tmp_path, arch):
    (tmp_path / "mix.exs").write_text("defmodule Bookmarks.MixProject do\nend\n")
    written = write(tmp_path, arch)
    text = (tmp_path / "test/fitness/architecture_test.exs").read_text()
    assert "defmodule Bookmarks.ArchitectureTest do" in text
    assert '{"tag-store-declared-dependencies", {:only_depends_on, Bookmarks.Tagging.TagStore, ' \
           '[Bookmarks.Storage], []}},' in text
    assert (tmp_path / "test/fitness/support/beck_fitness.ex").is_file()
    assert written.notes and "could not add test/fitness/support" in written.notes[0]


MIX_NEW = """defmodule Bookmarks.MixProject do
  use Mix.Project

  def project do
    [
      app: :bookmarks,
      version: "0.1.0",
      deps: deps()
    ]
  end

  defp deps do
    []
  end
end
"""


def test_mix_exs_gets_the_support_path_in_the_test_env():
    patched = compile_support(MIX_NEW)
    assert "    [\n      elixirc_paths: elixirc_paths(Mix.env()),\n      app: :bookmarks," in patched
    assert patched.endswith('  defp elixirc_paths(:test), do: ["lib", "test/fitness/support"]\n'
                            '  defp elixirc_paths(_), do: ["lib"]\nend\n')
    assert compile_support(patched) == patched


def test_an_existing_test_clause_gets_the_support_path_added():
    mix = 'defp elixirc_paths(:test), do: ["lib", "test/support"]'
    assert compile_support(mix) == 'defp elixirc_paths(:test), do: ["lib", "test/support", "test/fitness/support"]'


def test_an_inline_project_is_patched_and_an_unknown_shape_is_not():
    inline = "defmodule A do\n  def project, do: [app: :a]\nend\n"
    assert "def project, do: [elixirc_paths: elixirc_paths(Mix.env()), app: :a]" in compile_support(inline)
    assert compile_support('def project, do: [elixirc_paths: ["lib"]]') is None


def test_the_patched_mix_exs_is_written(tmp_path, arch):
    (tmp_path / "mix.exs").write_text(MIX_NEW)
    written = write(tmp_path, arch)
    assert "mix.exs" in written.files and written.notes == []
    assert "test/fitness/support" in (tmp_path / "mix.exs").read_text()


def test_a_rule_written_by_kent_is_no_longer_pending(python_project, arch):
    assert list(write(python_project, arch).pending_rules) == ["tagging-is-auditable"]
    (python_project / "tests/fitness/test_architecture_rules.py").write_text(
        "def test_audit():\n    # spec: tagging-is-auditable\n    assert True\n")
    assert write(python_project, arch).pending_rules == {}


def test_cli(python_project, capsys):
    (python_project / ".beck").mkdir()
    for name in ("domain.yaml", "architecture.yaml"):
        (python_project / ".beck" / name).write_text((EXAMPLE / name).read_text())
    assert main(["fitness", "generate", "--project", str(python_project)]) == 0
    out = capsys.readouterr().out
    assert "wrote tests/fitness/test_architecture.py" in out
    assert "untranslated: tagging-is-auditable" in out


def test_an_external_library_lives_outside_the_system(arch):
    arch["assertions"].append({"id": "tagging-has-no-json", "no_dependency": {"from": "tagging", "to": "json-codec"}})
    x = expand(arch)
    cases = by_id(x.cases)
    assert "json-codec-is-defined" not in cases
    assert "json-codec" not in cases["classes-only-in-components"].targets
    assert x.elements["json-codec"].namespace == ("json_codec",)
