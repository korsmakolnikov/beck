"""The DSL validator judges kent's output, so it is tested (README, "How beck itself is developed")."""

import copy
from pathlib import Path

import pytest
import yaml

from beck.cli import main
from beck.dsl.validate import validate_architecture, validate_domain

EXAMPLE = Path(__file__).parents[1] / "docs" / "examples" / "bookmarks"


@pytest.fixture
def domain():
    return yaml.safe_load((EXAMPLE / "domain.yaml").read_text())


@pytest.fixture
def arch():
    return yaml.safe_load((EXAMPLE / "architecture.yaml").read_text())


def messages(problems):
    return [str(p) for p in problems]


def test_the_example_is_valid(domain, arch):
    assert validate_domain(domain) == []
    assert validate_architecture(arch, domain) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d["morphisms"][0]["post"].__setitem__(0, "The bookmark carries tags."), "morphisms[0].post[0]"),
        (lambda d: d["morphisms"][0].pop("uses"), "'uses' is a required property"),
        (lambda d: d["morphisms"][0]["uses"].append("Ghost"), "morphisms[0].uses[3]: no entity named 'Ghost'"),
        (lambda d: d["morphisms"][0]["emits"].append("Ghost"), "no event named 'Ghost'"),
        (lambda d: d["constraints"][0]["about"].append("Ghost"), "constraints[0].about[2]: no entity named 'Ghost'"),
        (lambda d: d["relations"][0].__setitem__("to", "Ghost"), "relations[0].to: no entity named 'Ghost'"),
        (lambda d: d["properties"][0]["morphisms"].append("ghost"), "no morphism named 'ghost'"),
        (lambda d: d["morphisms"][1]["post"][0].__setitem__("id", "tag-bookmark"), "id 'tag-bookmark' is not unique"),
        (lambda d: d["events"][0].__setitem__("name", "Bookmark"), "name 'Bookmark' is not unique"),
        (lambda d: d["entities"][0]["fields"].append({"name": "id", "type": "integer"}), "field 'id' is not unique"),
    ],
)
def test_domain_problems(domain, mutate, expected):
    mutate(domain)
    assert any(expected in m for m in messages(validate_domain(domain))), messages(validate_domain(domain))


def tagger(arch):
    return arch["elements"][0]["elements"][0]


def store(arch):
    return arch["elements"][0]["elements"][1]


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda a: a["elements"][0].__setitem__("implements", ["tag-bookmark"]), "elements[0]"),
        (lambda a: tagger(a).pop("boundary"), "'boundary' is a required property"),
        (lambda a: a["assertions"][0].__setitem__("rule", "Two forms."), "assertions[0]"),
        (lambda a: a["assertions"][2]["foreach"].__setitem__("assert", {"rule": "No."}), "assertions[2]"),
        (lambda a: tagger(a)["depends_on"].append("ghost"), "no component or library with id 'ghost'"),
        (lambda a: tagger(a)["depends_on"].append("tagging"), "no component or library with id 'tagging'"),
        (lambda a: tagger(a)["depends_on"].append("bookmark-tagger"), "cannot depend on itself"),
        (lambda a: store(a)["depends_on"].append("bookmark-tagger"), "dependency cycle"),
        (lambda a: a["assertions"][0]["no_dependency"].__setitem__("to", "ghost"), "no element with id 'ghost'"),
        (lambda a: a["assertions"][0]["no_dependency"].__setitem__("to", "$c"), "$c is not a variable"),
        (lambda a: a["assertions"][2]["foreach"].__setitem__("within", "storage"), "contains no elements"),
        (lambda a: tagger(a)["implements"].append("tag-missing-bookmark"), "comes with its morphism"),
        (lambda a: tagger(a)["implements"].append("ghost"), "no domain id 'ghost'"),
        (lambda a: store(a)["implements"].append("tag-bookmark"), "implemented id 'tag-bookmark' is not unique"),
        (lambda a: a.pop("integration"), "'tagging-is-atomic' is implemented by no component"),
        (lambda a: a["constants"][0].__setitem__("id", "tag-bookmark"), "already a domain id"),
        (lambda a: store(a).__setitem__("id", "bookmark-tagger"), "id 'bookmark-tagger' is not unique"),
    ],
)
def test_architecture_problems(domain, arch, mutate, expected):
    mutate(arch)
    found = messages(validate_architecture(arch, domain))
    assert any(expected in m for m in found), found


def test_an_invalid_domain_stops_the_architecture(domain, arch):
    domain["morphisms"][0]["uses"].append("Ghost")
    assert messages(validate_architecture(arch, domain)) == ["<root>: the domain is not valid: validate it first"]


def test_cli(tmp_path, capsys, domain, arch):
    beck_dir = tmp_path / ".beck"
    beck_dir.mkdir()
    (beck_dir / "domain.yaml").write_text(yaml.safe_dump(domain))
    (beck_dir / "architecture.yaml").write_text(yaml.safe_dump(arch))
    assert main(["validate", "architecture", "--project", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "ok\n"
    broken = copy.deepcopy(arch)
    broken.pop("integration")
    (beck_dir / "architecture.yaml").write_text(yaml.safe_dump(broken))
    assert main(["validate", "architecture", "--project", str(tmp_path)]) == 1
    assert "implemented by no component" in capsys.readouterr().out


@pytest.mark.parametrize("mutate, expected", [
    (lambda a: a["elements"][0].__setitem__("supervised", True), "elements[0]"),
    (lambda a: tagger(a).__setitem__("package", "x"), "elements[0].elements[0]"),
    (lambda a: a["elements"][3].pop("package"), "elements[3]"),
])
def test_supervised_and_package_belong_to_their_kinds(domain, arch, mutate, expected):
    mutate(arch)
    assert any(expected in m for m in messages(validate_architecture(arch, domain)))


def test_the_old_realizes_key_is_named_with_its_new_name(domain, arch):
    # the user, 2026-11-16: a component implements its ids; `realizes` is renamed `implements` everywhere
    from beck.dsl.validate import validate_architecture
    tagger(arch)["realizes"] = tagger(arch).pop("implements")
    arch["integration"]["realizes"] = arch["integration"].pop("implements")
    problems = [p.message for p in validate_architecture(arch, domain)]
    assert problems == ["`realizes` is now `implements`: rename the key", "`realizes` is now `implements`: rename the key"]
