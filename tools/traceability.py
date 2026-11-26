"""Traceability: which test or manual check verifies each requirement (REQ-45).

Reads three sources:

- docs/engineering/requirements.md: every REQ ID and its text
- tests/test_*.py: test docstrings starting with "Checks REQ-xx" (or several IDs)
- docs/engineering/verification.md: the manual checks table

and works out a status per REQ:

- deferred: in the Deferred section of the requirements page (not built yet)
- tests: at least one test names it
- manual: it has manual check rows, every one with a result
- pending: it has manual check rows, at least one still without a result
- not verifiable: no test and no manual check row

Used three ways:

- tests/test_traceability.py fails CI on a REQ that is not verifiable, or an unknown ID
- `uv run python -m tools.traceability` prints a short summary (always exit code 0)
- MkDocs loads this file as a hook (mkdocs.yml `hooks:`) and puts the table on the
  Verification page during the build
"""

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REQ_ID = re.compile(r"REQ-\d+")

# Statuses, in the order they are checked and printed.
DEFERRED = "deferred"
TESTS = "tests"
MANUAL = "manual"
PENDING = "pending"
NOT_VERIFIABLE = "not verifiable"
STATUSES = [TESTS, MANUAL, PENDING, DEFERRED, NOT_VERIFIABLE]

# Where the files are, relative to the repository root.
REQUIREMENTS_FILE = Path("docs/engineering/requirements.md")
VERIFICATION_FILE = Path("docs/engineering/verification.md")
TESTS_DIR = Path("tests")

# The Verification page has this line where the table goes.
TABLE_MARKER = "<!-- traceability-table -->"
VERIFICATION_PAGE = "engineering/verification.md"

SHORT_TEXT_LENGTH = 90


# --- 1. Requirements ----------------------------------------------------------


@dataclass
class Requirement:
    id: str
    short_text: str
    deferred: bool


def short_text(text):
    """The first sentence of a requirement, cut near SHORT_TEXT_LENGTH characters."""
    first = re.split(r"(?<=\.)\s", text.strip(), maxsplit=1)[0]
    if len(first) <= SHORT_TEXT_LENGTH:
        return first
    cut = first[:SHORT_TEXT_LENGTH].rsplit(" ", 1)[0]
    if cut.count("`") % 2 == 1:
        cut += "`"  # close a code span the cut left open
    return cut + " …"


def read_requirements(path):
    """Every REQ on the requirements page, in page order: {id: Requirement}.

    Rows look like `| **REQ-01** | text |`. Rows below the `## Deferred` heading are
    marked deferred.
    """
    requirements = {}
    deferred = False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            deferred = line[3:].strip().lower() == "deferred"
        match = re.match(r"\|\s*\*\*(REQ-\d+)\*\*\s*\|(.*)\|\s*$", line)
        if match:
            req_id, text = match.group(1), match.group(2)
            requirements[req_id] = Requirement(req_id, short_text(text), deferred)
    return requirements


# --- 2. Tests -----------------------------------------------------------------


@dataclass
class TestCheck:
    __test__ = False  # tell pytest this is not a test class

    file: str
    name: str
    req_ids: list


# The docstring starts with "Checks REQ-01" or "Checks REQ-01, REQ-02, ...".
CHECKS_LINE = re.compile(r"Checks\s+(REQ-\d+(?:\s*,\s*REQ-\d+)*)")


def read_tests(tests_dir):
    """Every test function whose docstring starts with "Checks REQ-xx"."""
    checks = []
    for test_file in sorted(Path(tests_dir).glob("test_*.py")):
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            match = CHECKS_LINE.match(ast.get_docstring(node) or "")
            if match:
                ids = REQ_ID.findall(match.group(1))
                checks.append(TestCheck(test_file.name, node.name, ids))
    return checks


# --- 3. Manual checks ---------------------------------------------------------


@dataclass
class ManualCheck:
    req_ids: list
    steps: str
    date: str
    result: str


def table_cells(line):
    """The cells of one markdown table row, without the outer pipes."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def read_manual_checks(path):
    """The rows of the table under `## Manual checks` on the Verification page.

    Columns are found by their header (REQ, Steps, Date, Result), so their order can
    change. A row may name several REQs ("REQ-39, REQ-40").
    """
    checks = []
    in_section = False
    header = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == "manual checks"
            header = None
            continue
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = table_cells(line)
        if header is None:
            header = [cell.lower() for cell in cells]
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue  # the |---|---| line
        row = dict(zip(header, cells))
        checks.append(
            ManualCheck(
                req_ids=REQ_ID.findall(row.get("req", "")),
                steps=row.get("steps", ""),
                date=row.get("date", ""),
                result=row.get("result", ""),
            )
        )
    return checks


# --- 4. Status per requirement ------------------------------------------------


@dataclass
class Trace:
    requirement: Requirement
    tests: list  # TestCheck
    manual: list  # ManualCheck
    status: str


def work_out_status(requirements, tests, manual_checks):
    """One Trace per requirement, in page order."""
    traces = []
    for req in requirements.values():
        req_tests = [t for t in tests if req.id in t.req_ids]
        req_manual = [m for m in manual_checks if req.id in m.req_ids]
        if req.deferred:
            status = DEFERRED
        elif req_tests:
            status = TESTS
        elif req_manual and all(m.result for m in req_manual):
            status = MANUAL
        elif req_manual:
            status = PENDING
        else:
            status = NOT_VERIFIABLE
        traces.append(Trace(req, req_tests, req_manual, status))
    return traces


def unknown_ids(requirements, tests, manual_checks):
    """IDs named by a test or manual check that are not on the requirements page.

    Returns a list of (id, where) pairs, where says which test or manual row.
    """
    unknown = []
    for test in tests:
        for req_id in test.req_ids:
            if req_id not in requirements:
                unknown.append((req_id, f"{test.file}::{test.name}"))
    for check in manual_checks:
        for req_id in check.req_ids:
            if req_id not in requirements:
                unknown.append((req_id, f"manual check: {check.steps}"))
    return unknown


def trace_repo(root):
    """Read the three sources of the repository at root and work out every status."""
    root = Path(root)
    requirements = read_requirements(root / REQUIREMENTS_FILE)
    tests = read_tests(root / TESTS_DIR)
    manual_checks = read_manual_checks(root / VERIFICATION_FILE)
    traces = work_out_status(requirements, tests, manual_checks)
    unknown = unknown_ids(requirements, tests, manual_checks)
    return traces, unknown


# --- 5. Markdown table --------------------------------------------------------


def escape_cell(text):
    """Make text safe inside a markdown table cell."""
    return text.replace("|", "\\|")


def manual_result(check):
    """One manual check as shown in the table: "Pass 19.11.2026", or "open"."""
    if not check.result:
        return "open"
    return f"{check.result} {check.date}".strip()


def traceability_table(traces):
    """The traceability table as markdown, one row per requirement."""
    lines = [
        "| REQ | Requirement | Tests | Manual check | Status |",
        "|---|---|---|---|---|",
    ]
    for trace in traces:
        tests = "<br>".join(f"`{t.name}`" for t in trace.tests)
        manual = "<br>".join(escape_cell(manual_result(m)) for m in trace.manual)
        lines.append(
            f"| {trace.requirement.id} | {escape_cell(trace.requirement.short_text)} "
            f"| {tests} | {manual} | {trace.status} |"
        )
    return "\n".join(lines)


# --- 6. MkDocs hook -----------------------------------------------------------
# MkDocs calls this for every page during the build (mkdocs.yml: hooks:).


def on_page_markdown(markdown, page, config, files):
    """Put the generated table on the Verification page, in place of the marker."""
    if page.file.src_uri != VERIFICATION_PAGE:
        return markdown
    if TABLE_MARKER not in markdown:
        raise ValueError(f"{VERIFICATION_PAGE} has no {TABLE_MARKER} line")
    root = Path(config.config_file_path).parent
    traces, _ = trace_repo(root)
    return markdown.replace(TABLE_MARKER, traceability_table(traces))


# --- 7. Summary on the command line -------------------------------------------
# uv run python -m tools.traceability


def summary(traces, unknown):
    """A short text summary: counts per status and what is still open."""
    lines = ["Traceability summary", ""]
    for status in STATUSES:
        count = sum(1 for t in traces if t.status == status)
        lines.append(f"  {status:<15} {count}")
    lines.append(f"  {'total':<15} {len(traces)}")

    def section(title, items):
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(f"  {item}" for item in items or ["none"])

    pending = []
    for trace in traces:
        if trace.status == PENDING:
            for check in trace.manual:
                if not check.result:
                    pending.append(f"{trace.requirement.id}: {check.steps}")
    section("Pending (manual check without a result)", pending)

    section(
        "Deferred (not built yet)",
        [
            f"{t.requirement.id}: {t.requirement.short_text}"
            for t in traces
            if t.status == DEFERRED
        ],
    )

    # Each open manual check once, with all the REQs it names (a row may name several).
    other_open = []
    for trace in traces:
        if trace.status in (TESTS, MANUAL):
            for check in trace.manual:
                line = f"{', '.join(check.req_ids)}: {short_text(check.steps)}"
                if not check.result and line not in other_open:
                    other_open.append(line)
    section("Other manual checks without a result (REQ verified by tests)", other_open)

    section(
        "Not verifiable (no test, no manual check)",
        [
            f"{t.requirement.id}: {t.requirement.short_text}"
            for t in traces
            if t.status == NOT_VERIFIABLE
        ],
    )
    section("Unknown REQ IDs", [f"{req_id} in {where}" for req_id, where in unknown])
    return "\n".join(lines)


def main():
    """Print the summary for this repository. Always exit code 0: it is for reading."""
    root = Path(__file__).resolve().parent.parent
    traces, unknown = trace_repo(root)
    print(summary(traces, unknown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
