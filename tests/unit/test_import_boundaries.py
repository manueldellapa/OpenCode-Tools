"""Executable import-boundary contract for the package's dependency graph.

Builds the direct-import graph of a package tree via `ast` and checks the
boundaries fixed by System Design SS5.3: `domain.py` never imports an
application module, and no low-level adapter imports `orchestrator.py` or
reaches into the OpenCode-specific knowledge reserved for `process.py`.
Fixture packages exercise the same graph and rule functions against
synthetic violations, independently of which adapters already exist.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Final

SOURCE_ROOT: Final = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT: Final = SOURCE_ROOT / "opencode_tools"
PACKAGE_NAME: Final = "opencode_tools"

LOW_LEVEL_MODULES: Final = frozenset(
    {"process", "git_safety", "github", "opencode", "runlog", "locking"}
)
PROCESS_FORBIDDEN_MODULES: Final = frozenset({"opencode", "git_safety", "protocol"})


def _module_name(source_file: Path, *, source_root: Path) -> str:
    relative_path = source_file.relative_to(source_root).with_suffix("")
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
            prefix = module if module.endswith(".") else f"{module}."
            imports.update(f"{prefix}{alias.name}" for alias in node.names)

    return frozenset(imports)


def _import_graph(source_root: Path, package_root: Path) -> dict[str, frozenset[str]]:
    return {
        _module_name(source_file, source_root=source_root): _direct_imports(source_file)
        for source_file in sorted(package_root.rglob("*.py"))
    }


def _is_internal_import(imported: str) -> bool:
    return imported == PACKAGE_NAME or imported.startswith((f"{PACKAGE_NAME}.", "."))


def _refers_to_module(imported: str, short_name: str) -> bool:
    return imported == f"{PACKAGE_NAME}.{short_name}" or imported.endswith(
        f".{short_name}"
    )


def _domain_boundary_violations(graph: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """Application-module imports found on `domain.py`, if any."""
    domain_imports = graph.get(f"{PACKAGE_NAME}.domain", frozenset())
    return frozenset(
        imported for imported in domain_imports if _is_internal_import(imported)
    )


def _orchestrator_boundary_violations(
    graph: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Low-level modules that import `orchestrator.py`, keyed by module name."""
    violations: dict[str, frozenset[str]] = {}
    for module_name in LOW_LEVEL_MODULES:
        qualified_name = f"{PACKAGE_NAME}.{module_name}"
        imports = graph.get(qualified_name)
        if imports is None:
            continue
        matches = frozenset(
            imported
            for imported in imports
            if _refers_to_module(imported, "orchestrator")
        )
        if matches:
            violations[qualified_name] = matches
    return violations


def _process_boundary_violations(graph: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """OpenCode-layer imports found on `process.py`, if any."""
    process_imports = graph.get(f"{PACKAGE_NAME}.process", frozenset())
    return frozenset(
        imported
        for imported in process_imports
        if any(_refers_to_module(imported, name) for name in PROCESS_FORBIDDEN_MODULES)
    )


def _write_fixture_package(tmp_path: Path, files: Mapping[str, str]) -> Path:
    package_root = tmp_path / PACKAGE_NAME
    package_root.mkdir()
    for name, content in files.items():
        (package_root / name).write_text(content, encoding="utf-8")
    return package_root


def test_import_graph_discovers_the_real_package() -> None:
    graph = _import_graph(SOURCE_ROOT, PACKAGE_ROOT)

    assert graph
    assert PACKAGE_NAME in graph


def test_real_domain_module_has_no_application_imports() -> None:
    graph = _import_graph(SOURCE_ROOT, PACKAGE_ROOT)

    assert _domain_boundary_violations(graph) == frozenset()


def test_real_graph_has_no_low_level_orchestrator_imports() -> None:
    graph = _import_graph(SOURCE_ROOT, PACKAGE_ROOT)

    assert _orchestrator_boundary_violations(graph) == {}


def test_real_graph_has_no_process_opencode_knowledge() -> None:
    graph = _import_graph(SOURCE_ROOT, PACKAGE_ROOT)

    assert _process_boundary_violations(graph) == frozenset()


def test_domain_boundary_flags_application_import_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {
            "domain.py": "from opencode_tools.errors import PipelineError\n",
            "errors.py": "",
        },
    )

    graph = _import_graph(tmp_path, package_root)

    assert _domain_boundary_violations(graph) == frozenset(
        {f"{PACKAGE_NAME}.errors", f"{PACKAGE_NAME}.errors.PipelineError"}
    )


def test_domain_boundary_accepts_stdlib_only_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {"domain.py": "from dataclasses import dataclass\nfrom pathlib import Path\n"},
    )

    graph = _import_graph(tmp_path, package_root)

    assert _domain_boundary_violations(graph) == frozenset()


def test_orchestrator_boundary_flags_low_level_import_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {
            "orchestrator.py": "",
            "process.py": "from opencode_tools.orchestrator import run_issue\n",
        },
    )

    graph = _import_graph(tmp_path, package_root)

    assert _orchestrator_boundary_violations(graph) == {
        f"{PACKAGE_NAME}.process": frozenset({f"{PACKAGE_NAME}.orchestrator"})
    }


def test_orchestrator_boundary_flags_relative_import_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {"orchestrator.py": "", "github.py": "from . import orchestrator\n"},
    )

    graph = _import_graph(tmp_path, package_root)

    assert _orchestrator_boundary_violations(graph) == {
        f"{PACKAGE_NAME}.github": frozenset({".orchestrator"})
    }


def test_orchestrator_boundary_accepts_valid_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {
            "orchestrator.py": "from opencode_tools.process import ProcessRunner\n",
            "process.py": "from opencode_tools.domain import ProcessSpec\n",
        },
    )

    graph = _import_graph(tmp_path, package_root)

    assert _orchestrator_boundary_violations(graph) == {}


def test_process_boundary_flags_opencode_knowledge_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {
            "process.py": "from opencode_tools.opencode import AgentResult\n",
            "opencode.py": "",
        },
    )

    graph = _import_graph(tmp_path, package_root)

    assert _process_boundary_violations(graph) == frozenset(
        {f"{PACKAGE_NAME}.opencode"}
    )


def test_process_boundary_accepts_valid_fixture(tmp_path: Path) -> None:
    package_root = _write_fixture_package(
        tmp_path,
        {
            "process.py": (
                "from opencode_tools.domain import ProcessSpec\n"
                "from opencode_tools.ports import Clock\n"
            )
        },
    )

    graph = _import_graph(tmp_path, package_root)

    assert _process_boundary_violations(graph) == frozenset()
