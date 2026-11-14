"""The summary the agent reads instead of the console: built deterministically from the JUnit XML."""

import pytest

from beck.junit import Case, Report, UnreadableReport, parse, summary

XML = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.test_a" name="test_ok" time="0.01"/>
  <testcase classname="tests.test_a.TestX" name="test_fails" time="0.01">
    <failure message="AssertionError: assert 1 == 2">def test_fails():
&gt;       assert 1 == 2
E       AssertionError: assert 1 == 2

tests/test_a.py:7: AssertionError</failure></testcase>
  <testcase classname="tests.test_a" name="test_raises" time="0.01">
    <error message="NotImplementedError">src/pkg/mod.py:3: NotImplementedError</error></testcase>
  <testcase classname="tests.test_a" name="test_skipped"><skipped message="later"/></testcase>
</testsuite></testsuites>
"""


@pytest.fixture
def report(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text("")
    (tmp_path / "r.xml").write_text(XML)
    return parse(tmp_path / "r.xml", tmp_path)


def test_cases_get_ids_outcomes_messages_and_locations(report):
    assert report.cases == (
        Case("tests/test_a.py::test_ok", "passed"),
        Case("tests/test_a.py::TestX::test_fails", "failure", "AssertionError: assert 1 == 2", "tests/test_a.py:7"),
        Case("tests/test_a.py::test_raises", "error", "NotImplementedError", "src/pkg/mod.py:3"),
        Case("tests/test_a.py::test_skipped", "skipped", "later", None),
    )
    assert report.counts() == {"tests": 4, "passed": 1, "failure": 1, "error": 1, "skipped": 1}


def test_the_summary_lists_only_what_did_not_pass(report):
    assert summary(report) == (
        "NOT_OK (2 of 4 not passed)\n"
        "- tests/test_a.py::TestX::test_fails [failure] at tests/test_a.py:7\n"
        "    AssertionError: assert 1 == 2\n"
        "- tests/test_a.py::test_raises [error] at src/pkg/mod.py:3\n"
        "    NotImplementedError")


def test_all_green_is_one_line():
    assert summary(Report((Case("a", "passed"), Case("b", "passed")))) == "ALL_OK (2 passed)"
    assert summary(Report((Case("a", "passed"), Case("b", "skipped")))) == "ALL_OK (1 passed, 1 skipped)"


def test_a_long_message_is_cut(tmp_path):
    long = "x" * 1000
    (tmp_path / "r.xml").write_text(f'<testsuite><testcase name="t"><failure message="{long}"/></testcase></testsuite>')
    message = parse(tmp_path / "r.xml", tmp_path).cases[0].message
    assert len(message) == 301 and message.endswith("…")


def test_an_unreadable_report(tmp_path):
    (tmp_path / "r.xml").write_text("<testsuite>")
    with pytest.raises(UnreadableReport):
        parse(tmp_path / "r.xml", tmp_path)
