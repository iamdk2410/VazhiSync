"""backend/CLAUDE.md: `simulator/` must not import from `app/` (keeps it pure and reusable)."""

import ast
from pathlib import Path

SIMULATOR = Path(__file__).resolve().parents[2] / "simulator"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_simulator_does_not_import_app() -> None:
    offenders = [p for p in SIMULATOR.rglob("*.py") if "app" in _imported_roots(p)]
    assert offenders == []


SCENARIO_GEN = SIMULATOR.parent / "scenario_gen"


def test_scenario_gen_does_not_import_app() -> None:
    """The scenario director stays reusable: the app injects the LLM client and storage."""
    offenders = [p for p in SCENARIO_GEN.rglob("*.py") if "app" in _imported_roots(p)]
    assert offenders == []
