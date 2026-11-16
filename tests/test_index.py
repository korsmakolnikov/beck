"""The index decides what each agent sees, so it is tested on the bookmarks example."""

from pathlib import Path

import pytest
import yaml

from beck.cli import main
from beck.index import execution
from beck.index.build import build
from beck.index.store import INTEGRATION

EXAMPLE = Path(__file__).parents[1] / "docs" / "examples" / "bookmarks"

CODE = {
    "lib/bookmarks/tagging/bookmark_tagger.ex": '''
defmodule Bookmarks.Tagging.BookmarkTagger do
  @doc "Tags a bookmark. spec: tag-bookmark"
  @callback tag(integer, [String.t()]) :: :ok | {:error, :missing}

  # spec: tag-belongs-to-the-same-user
  @impl true
  def check_owner(bookmark, tags) when is_list(tags), do: :ok
end
''',
    "test/bookmark_tagger_test.exs": '''
defmodule Bookmarks.Tagging.BookmarkTaggerTest do
  use ExUnit.Case

  test "a missing bookmark changes nothing" do
    # spec: tag-missing-bookmark
    assert true
  end
end
''',
    "src/bookmarks/tagging/tag_store.py": '''
class TagStore:
    def add(self, name: str) -> None:
        """Stores a tag. spec: tag-name-is-1-to-40-characters"""

# spec: ghost-id
x = 1
''',
    "src/bookmarks/archive/archiver.py": '''
# spec: archive-bookmark
@staticmethod
def archive(bookmark_id: int) -> None:
    ...
''',
    "tests/test_tag_store.py": '''
def test_a_long_name_is_refused():
    # spec: tag-name-is-1-to-40-characters
    assert True
''',
    "include/archiver.h": '''
/* spec: archived-bookmark-has-a-tag */
int archiver_check(int bookmark_id);
''',
}


def docs():
    return (yaml.safe_load((EXAMPLE / "domain.yaml").read_text()),
            yaml.safe_load((EXAMPLE / "architecture.yaml").read_text()))


@pytest.fixture
def project(tmp_path):
    for path, text in CODE.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    (tmp_path / ".beck").mkdir()
    for name in ("domain.yaml", "architecture.yaml"):
        (tmp_path / ".beck" / name).write_text((EXAMPLE / name).read_text())
    return tmp_path


@pytest.fixture
def index(project):
    domain, arch = docs()
    conn, report = build(project, domain, arch, ":memory:")
    assert conn is not None, report.problems
    return conn, report


def edges(conn, kind):
    return {(r["src"], r["dst"]) for r in conn.execute("SELECT src, dst FROM edge WHERE kind = ?", (kind,))}


def test_invalid_dsls_build_no_index(project):
    domain, arch = docs()
    arch.pop("integration")
    conn, report = build(project, domain, arch, ":memory:")
    assert conn is None and report.problems


def test_a_morphism_brings_its_requirements(index):
    conn, _ = index
    implemented = {dst for src, dst in edges(conn, "implements") if src == "bookmark-tagger"}
    assert implemented == {"tag-bookmark", "tagged-bookmark-carries-the-requested-tags", "tag-missing-bookmark",
                        "tagging-twice-equals-tagging-once", "tag-belongs-to-the-same-user"}


def test_components_land_dependencies_first_and_integration_last(index):
    conn, _ = index
    components = execution.components(conn)
    assert components.index("tag-store") < components.index("bookmark-tagger")
    assert components.index("tag-store") < components.index("archiver")
    assert components[-1] == INTEGRATION


def test_units_go_from_the_particular_to_the_general(index):
    conn, _ = index
    assert execution.units(conn, "bookmark-tagger") == [
        # a property is an invariant: of its entity, in the entity's model unit
        execution.Unit("model:Bookmark", "Bookmark", ("tag-belongs-to-the-same-user",
                                                      "tagging-twice-equals-tagging-once")),
        execution.Unit("morphism:tag-bookmark", "tag-bookmark",
                       ("tagged-bookmark-carries-the-requested-tags", "tag-missing-bookmark")),
    ]
    assert execution.units(conn, INTEGRATION) == [execution.Unit("component", None, ("tagging-is-atomic",))]


@pytest.mark.parametrize("id_, symbol", [
    ("tag-bookmark", "lib/bookmarks/tagging/bookmark_tagger.ex::defmodule Bookmarks.Tagging.BookmarkTagger.callback tag/2"),
    ("tag-belongs-to-the-same-user",
     "lib/bookmarks/tagging/bookmark_tagger.ex::defmodule Bookmarks.Tagging.BookmarkTagger.def check_owner/2"),
    ("tag-name-is-1-to-40-characters", "src/bookmarks/tagging/tag_store.py::TagStore.add"),
    ("archive-bookmark", "src/bookmarks/archive/archiver.py::archive"),
    ("archived-bookmark-has-a-tag", "include/archiver.h::archiver_check"),
])
def test_a_marker_anchors_to_its_definition(index, id_, symbol):
    conn, _ = index
    assert (id_, symbol) in edges(conn, "anchored_to")


def test_a_marker_in_a_test_file_marks_a_test(index):
    conn, _ = index
    assert edges(conn, "tests") == {
        ('test/bookmark_tagger_test.exs::defmodule Bookmarks.Tagging.BookmarkTaggerTest.'
         'test "a missing bookmark changes nothing"', "tag-missing-bookmark"),
        ("tests/test_tag_store.py::test_a_long_name_is_refused", "tag-name-is-1-to-40-characters"),
    }


def test_the_report_names_unknown_ids_and_unanchored_morphisms(project):
    (project / "src/bookmarks/archive/archiver.py").unlink()
    domain, arch = docs()
    _, report = build(project, domain, arch, ":memory:")
    assert [(m.id, m.path) for m in report.unknown] == [("ghost-id", "src/bookmarks/tagging/tag_store.py")]
    assert report.unanchored == ["archive-bookmark"]


def test_the_context_holds_only_the_unit(index):
    conn, _ = index
    ctx = execution.context(conn, "bookmark-tagger", "morphism:tag-bookmark")
    assert [c["id"] for c in ctx["requirements"]] == ["tagged-bookmark-carries-the-requested-tags",
                                                      "tag-missing-bookmark"]
    assert [e["name"] for e in ctx["entities"]] == ["Bookmark", "Tag", "TagName"]
    assert ctx["morphism"]["symbols"][0]["signature"].startswith("@callback tag(")
    assert [e["name"] for e in ctx["morphism"]["emits"]] == ["BookmarkTagged"]
    assert [d["id"] for d in ctx["dependencies"]] == ["tag-store"]
    assert ctx["files"] == {"source": ["lib/bookmarks/tagging/bookmark_tagger.ex"],
                            "tests": ["test/bookmark_tagger_test.exs"]}
    assert "archive-bookmark" not in str(ctx)


def test_an_unknown_unit_is_refused(index):
    conn, _ = index
    with pytest.raises(KeyError):
        execution.context(conn, "bookmark-tagger", "morphism:archive-bookmark")


def test_cli(project, capsys):
    assert main(["index", "build", "--project", str(project)]) == 0
    assert "unknown id: src/bookmarks/tagging/tag_store.py:6: spec: ghost-id" in capsys.readouterr().out
    assert main(["index", "execution", "--project", str(project)]) == 0
    assert "  morphism:tag-bookmark\n    tagged-bookmark-carries-the-requested-tags" in capsys.readouterr().out
    assert main(["index", "context", "tag-store", "model:TagName", "--project", str(project)]) == 0
    assert '"tag-name-is-1-to-40-characters"' in capsys.readouterr().out


def test_a_file_with_symbols_of_two_components_is_shared(project):
    (project / "src/bookmarks/common.py").write_text(
        "# spec: tag-bookmark\ndef a(): ...\n\n# spec: archive-bookmark\ndef b(): ...\n")
    domain, arch = docs()
    conn, _ = build(project, domain, arch, ":memory:")
    assert execution.shared_files(conn) == {"src/bookmarks/common.py": ["archiver", "bookmark-tagger"]}
    assert execution.exclusive(conn) == {frozenset(("archiver", "bookmark-tagger"))}


def test_no_file_is_shared_when_each_holds_one_component(index):
    conn, _ = index
    assert execution.shared_files(conn) == {} and execution.exclusive(conn) == set()


def test_a_component_waits_only_for_its_concrete_dependencies(project):
    from beck.machine.session import derive_execution
    domain, arch = docs()
    conn, _ = build(project, domain, arch, ":memory:")
    assert not any("waits_for" in u for u in derive_execution(conn)["components"])   # tag-store is an interface
    store = next(c for d in arch["elements"] for c in d.get("elements", []) if c.get("id") == "tag-store")
    store["boundary"] = "concrete"
    conn, _ = build(project, domain, arch, ":memory:")
    waits = {u["id"]: u.get("waits_for") for u in derive_execution(conn)["components"]}
    assert waits["bookmark-tagger"] == ["tag-store"] and waits["archiver"] == ["tag-store"]
    assert waits["tag-store"] is None
