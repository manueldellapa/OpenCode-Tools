"""Scaffold for the package import-boundary checks."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

SOURCE_ROOT: Final = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT: Final = SOURCE_ROOT / "opencode_tools"


def _module_name(source_file: Path) -> str:
    relative_path = source_file.relative_to(SOURCE_ROOT).with_suffix("")
    parts = relative_path.parts
    if relative_path.name == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _direct_imports(source_file: Path) -> frozenset[str]:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    imports: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = f"{'.' * node.level}{node.module or ''}"
            imports.add(module)

    return frozenset(imports)


def _import_graph() -> dict[str, frozenset[str]]:
    return {
        _module_name(source_file): _direct_imports(source_file)
        for source_file in sorted(PACKAGE_ROOT.rglob("*.py"))
    }


def test_import_boundary_scaffold_discovers_the_package_graph() -> None:
    graph = _import_graph()

    assert graph
    assert "opencode_tools" in graph
