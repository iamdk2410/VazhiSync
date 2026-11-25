"""B17: every row of docs/failure-matrix.md has a passing automated test - checked here.

Each row's Test column must name at least one test as `file.py::test_name` (a short file name
is looked up under tests/), "same test(s)" (the row above), or a CI step (`CI step "<name>"`).
The named test functions must exist; running the suite proves they pass. And every failure
the course brief lists must have a row.
"""

import ast
import re
from functools import cache
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
MATRIX = REPO / "docs" / "failure-matrix.md"
CI = REPO / ".github" / "workflows" / "ci.yml"
TESTS = BACKEND / "tests"

REF = re.compile(r"(?:(?P<file>[\w./-]*test_\w+\.py))?::(?P<test>test_\w+)")
CI_STEP = re.compile(r'CI step "(?P<step>[^"]+)"')

# The course brief's failure list -> words that must appear in some row's Failure column.
BRIEF = {
    "wrong tool chosen": "Wrong tool chosen",
    "invalid tool output": "Invalid tool output",
    "tool timeout": "Tool timeout",
    "MCP disconnect": "MCP server down / disconnect",
    "LLM timeout or rate limit": "Free-tier rate limit (429)",
    "unavailable model": "Bad or missing API key / unknown model",
    "malformed response": "Malformed JSON / schema-invalid LLM output",
    "all providers down": "Every model down",
    "conflicting agent outputs": "Conflicting agent outputs",
    "duplicate send": "Duplicate send",
    "stale vehicle": "Bus went stale between approval and dispatch",
    "failed background job": "Failed background job",
}


def rows() -> list[tuple[str, str]]:
    """(failure, test column) for every row of every table in the matrix."""
    out = []
    for line in MATRIX.read_text("utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| Failure"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        out.append((cells[0], cells[-1]))
    return out


@cache
def functions_in(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text("utf-8"))
    return frozenset(
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
    )


def find(file: str | None, test: str) -> list[Path]:
    if file:
        name = Path(file).name
        candidates = [p for p in TESTS.rglob(name)]
    else:
        candidates = list(TESTS.rglob("test_*.py"))
    return [p for p in candidates if test in functions_in(p)]


def ci_steps() -> set[str]:
    return set(re.findall(r"^\s*- name: (.+)$", CI.read_text("utf-8"), flags=re.M))


def resolved_refs() -> list[tuple[str, list[str]]]:
    """Each row with its references ('same test' resolved to the row above)."""
    out: list[tuple[str, list[str]]] = []
    previous: list[str] = []
    for failure, column in rows():
        refs = [f"{m['file'] or ''}::{m['test']}" for m in REF.finditer(column)]
        refs += [f"ci::{m['step']}" for m in CI_STEP.finditer(column)]
        if not refs and column.lower().startswith("same test"):
            refs = previous
        out.append((failure, refs))
        previous = refs
    return out


def test_every_row_names_an_automated_test() -> None:
    missing = [failure for failure, refs in resolved_refs() if not refs]
    assert missing == [], "rows without a test:\n- " + "\n- ".join(missing)


def test_every_named_test_exists() -> None:
    steps = ci_steps()
    broken: list[str] = []
    for failure, refs in resolved_refs():
        for ref in refs:
            if ref.startswith("ci::"):
                if ref[4:] not in steps:
                    broken.append(f"{failure[:60]} -> CI step {ref[4:]!r}")
                continue
            file, test = ref.split("::")
            if not find(file or None, test):
                broken.append(f"{failure[:60]} -> {ref}")
    assert broken == [], "references to tests that don't exist:\n- " + "\n- ".join(broken)


@pytest.mark.parametrize(("brief", "row"), sorted(BRIEF.items()))
def test_every_failure_in_the_brief_has_a_row(brief: str, row: str) -> None:
    failures = [failure for failure, _ in rows()]
    assert any(row.lower() in f.lower() for f in failures), f"no row for {brief!r} ({row!r})"


def test_the_matrix_is_big_enough_to_mean_something() -> None:
    assert len(rows()) > 100
