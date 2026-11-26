"""Tests for tools/traceability.py (REQ-45).

The first test checks the real repository, so CI fails when a requirement has no test
and no manual check, or when something names a REQ ID that does not exist. The others
run the tool on a small fake repository written in tmp_path.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import traceability

REPO = Path(__file__).resolve().parent.parent


# --- The real repository ------------------------------------------------------


def test_every_requirement_is_verifiable():
    """Checks REQ-45: every REQ has a test or a manual check, and no test or manual
    check names an unknown REQ ID. Pending and deferred are allowed."""
    traces, unknown = traceability.trace_repo(REPO)
    assert traces, "no requirements found"
    not_verifiable = [
        t.requirement.id for t in traces if t.status == traceability.NOT_VERIFIABLE
    ]
    assert not_verifiable == [], f"no test and no manual check: {not_verifiable}"
    assert unknown == [], f"unknown REQ IDs: {unknown}"


# --- A small fake repository --------------------------------------------------
# REQ-01 tests, REQ-02 manual, REQ-03 pending, REQ-04 not verifiable,
# REQ-05 deferred. REQ-99 is named by a test and a manual row but does not exist.

REQUIREMENTS = """# Requirements

## Things

| ID | Requirement |
|---|---|
| **REQ-01** | Tested by a test. More text. |
| **REQ-02** | Checked by hand, with a result. |
| **REQ-03** | Checked by hand, `no result` yet. |
| **REQ-04** | Nothing checks this one. |

## Deferred

| ID | Requirement |
|---|---|
| **REQ-05** | Not built yet. |
"""

TEST_FILE = '''
def test_one():
    """Checks REQ-01: something."""


def test_two():
    """Checks REQ-01, REQ-99: several IDs."""


def test_no_req():
    """Checks the project skeleton (M0)."""


def helper():
    """Checks REQ-04: not a test, so it does not count."""


class TestGroup:
    def test_in_class(self):
        """Checks REQ-05."""
'''

VERIFICATION = """# Verification

## Manual checks

| REQ | Steps | Expected | Date | Result |
|---|---|---|---|---|
| REQ-02 | press a button | it works | 01.01.2026 | Pass |
| REQ-02, REQ-03 | press another | it works | 02.01.2026 | Pass |
| REQ-03 | look at the phone | readable | | |
| REQ-99 | unknown | nothing | | |

## Traceability table

<!-- traceability-table -->
"""


@pytest.fixture
def fake_repo(tmp_path):
    """Writes the fake requirements page, test file and Verification page."""
    (tmp_path / "docs/engineering").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / traceability.REQUIREMENTS_FILE).write_text(REQUIREMENTS)
    (tmp_path / traceability.VERIFICATION_FILE).write_text(VERIFICATION)
    (tmp_path / "tests/test_fake.py").write_text(TEST_FILE)
    (tmp_path / "tests/helpers.py").write_text(
        'def test_x():\n    """Checks REQ-04."""\n'
    )
    return tmp_path


def statuses(root):
    traces, _ = traceability.trace_repo(root)
    return {t.requirement.id: t.status for t in traces}


def test_reads_requirements_and_deferred_section(fake_repo):
    """Checks REQ-45: every REQ row is read, in page order, with its first sentence;
    rows under Deferred are marked deferred."""
    reqs = traceability.read_requirements(fake_repo / traceability.REQUIREMENTS_FILE)
    assert list(reqs) == ["REQ-01", "REQ-02", "REQ-03", "REQ-04", "REQ-05"]
    assert reqs["REQ-01"].short_text == "Tested by a test."
    assert not reqs["REQ-04"].deferred
    assert reqs["REQ-05"].deferred


def test_short_text_is_cut_and_keeps_code_spans_closed():
    """Checks REQ-45: a long first sentence is cut at a word, closing an open `."""
    text = "Word " * 10 + "`some code that goes on and on and on and on and on` end."
    short = traceability.short_text(text)
    assert short.endswith(" …")
    assert len(short) <= traceability.SHORT_TEXT_LENGTH + 3
    assert short.count("`") % 2 == 0


def test_reads_test_docstrings(fake_repo):
    """Checks REQ-45: one or several IDs per test, test functions in classes too;
    other docstrings, helpers and files not named test_*.py are skipped."""
    tests = traceability.read_tests(fake_repo / "tests")
    found = {t.name: t.req_ids for t in tests}
    assert found == {
        "test_one": ["REQ-01"],
        "test_two": ["REQ-01", "REQ-99"],
        "test_in_class": ["REQ-05"],
    }
    assert {t.file for t in tests} == {"test_fake.py"}


def test_reads_manual_checks(fake_repo):
    """Checks REQ-45: each row of the manual checks table with its REQs, steps,
    date and result; a row may name several REQs."""
    checks = traceability.read_manual_checks(fake_repo / traceability.VERIFICATION_FILE)
    assert [c.req_ids for c in checks] == [
        ["REQ-02"],
        ["REQ-02", "REQ-03"],
        ["REQ-03"],
        ["REQ-99"],
    ]
    assert checks[0].steps == "press a button"
    assert checks[0].date == "01.01.2026"
    assert checks[0].result == "Pass"
    assert checks[2].result == ""


def test_status_per_requirement(fake_repo):
    """Checks REQ-45: tests, manual, pending, not verifiable and deferred."""
    assert statuses(fake_repo) == {
        "REQ-01": traceability.TESTS,
        "REQ-02": traceability.MANUAL,
        "REQ-03": traceability.PENDING,
        "REQ-04": traceability.NOT_VERIFIABLE,
        "REQ-05": traceability.DEFERRED,
    }


def test_unknown_ids_from_tests_and_manual_checks(fake_repo):
    """Checks REQ-45: a REQ ID named by a test or a manual row but missing from
    the requirements page is reported with where it came from."""
    _, unknown = traceability.trace_repo(fake_repo)
    assert unknown == [
        ("REQ-99", "test_fake.py::test_two"),
        ("REQ-99", "manual check: unknown"),
    ]


def test_table_has_one_row_per_requirement(fake_repo):
    """Checks REQ-45: ID, short text, test names, manual results and status."""
    traces, _ = traceability.trace_repo(fake_repo)
    table = traceability.traceability_table(traces).splitlines()
    assert table[0] == "| REQ | Requirement | Tests | Manual check | Status |"
    rows = table[2:]
    assert len(rows) == 5
    assert rows[0] == (
        "| REQ-01 | Tested by a test. | `test_one`<br>`test_two` |  | tests |"
    )
    assert "Pass 01.01.2026<br>Pass 02.01.2026" in rows[1]
    assert "Pass 02.01.2026<br>open" in rows[2]
    assert rows[3].endswith("| not verifiable |")
    assert rows[4].endswith("| deferred |")


def test_cell_text_with_a_pipe_is_escaped():
    """Checks REQ-45: a | in the text does not break the table."""
    assert traceability.escape_cell("a | b") == "a \\| b"


def mkdocs_args(root, src_uri):
    """The page and config arguments MkDocs passes to on_page_markdown."""
    page = SimpleNamespace(file=SimpleNamespace(src_uri=src_uri))
    config = SimpleNamespace(config_file_path=str(root / "mkdocs.yml"))
    return page, config


def test_hook_replaces_the_marker_on_the_verification_page(fake_repo):
    """Checks REQ-45: the MkDocs hook puts the table where the marker is."""
    page, config = mkdocs_args(fake_repo, traceability.VERIFICATION_PAGE)
    out = traceability.on_page_markdown(VERIFICATION, page, config, None)
    assert traceability.TABLE_MARKER not in out
    assert "| REQ-01 | Tested by a test." in out


def test_hook_leaves_other_pages_alone(fake_repo):
    """Checks REQ-45: other pages are returned unchanged."""
    page, config = mkdocs_args(fake_repo, "index.md")
    assert traceability.on_page_markdown("# Hi", page, config, None) == "# Hi"


def test_hook_fails_without_the_marker(fake_repo):
    """Checks REQ-45: a Verification page without the marker fails the build."""
    page, config = mkdocs_args(fake_repo, traceability.VERIFICATION_PAGE)
    with pytest.raises(ValueError, match="traceability-table"):
        traceability.on_page_markdown("# Verification", page, config, None)


def test_summary_counts_and_lists(fake_repo):
    """Checks REQ-45: counts per status, pending REQs with the missing manual check,
    deferred, not verifiable and unknown IDs."""
    traces, unknown = traceability.trace_repo(fake_repo)
    text = traceability.summary(traces, unknown)
    assert "tests           1" in text
    assert "pending         1" in text
    assert "total           5" in text
    assert "REQ-03: look at the phone" in text
    assert "REQ-05: Not built yet." in text
    assert "REQ-04: Nothing checks this one." in text
    assert "REQ-99 in test_fake.py::test_two" in text


def test_command_always_exits_0(capsys):
    """Checks REQ-45: the summary command prints and returns 0, for reading only."""
    assert traceability.main() == 0
    assert "Traceability summary" in capsys.readouterr().out
