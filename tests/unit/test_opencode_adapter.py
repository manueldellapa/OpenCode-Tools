"""Integrity, inventory, and provenance tests for the offline OpenCode
`1.17.18` fixture pack (M07-01).

This milestone builds evidence only: `tests/fixtures/opencode/1.17.18/` and
its `MANIFEST.json`. There is no `opencode_tools.opencode` adapter yet -- the
transport/capability/identity behavior the pack will anchor is implemented
and tested by later M07 work packages. What this module verifies is that the
pack itself is complete, internally consistent, attributable to the exact
candidate version, free of obvious secrets, and loadable deterministically
offline (System Design SS20.4, ADR-005).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Final, cast

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES_ROOT: Final = REPO_ROOT / "tests" / "fixtures" / "opencode" / "1.17.18"
MANIFEST_PATH: Final = FIXTURES_ROOT / "MANIFEST.json"
COMPATIBILITY_DOC_PATH: Final = REPO_ROOT / "docs" / "compatibility.md"

OPENCODE_VERSION: Final = "1.17.18"

_VALID_KINDS: Final = frozenset({"positive", "negative"})
_VALID_ROLES: Final = frozenset({"architect", "coder", "reviewer"})

# One category per version-sensitive behavior area System Design SS20.4 and
# issue #21 (M07-01) require offline evidence for.
_REQUIRED_CATEGORIES: Final = frozenset(
    {
        "run-transcript",
        "run-process-exit",
        "provider-trusted",
        "provider-lookalike",
        "transport-malformed",
        "debug-config",
        "debug-agent",
        "export-identity",
    }
)

# Fixtures whose entire point is to be malformed at the line/byte level; every
# other `.ndjson` fixture must be well-formed line-delimited JSON so a typo in
# a "clean" fixture is caught here rather than silently breaking a later
# milestone's parser tests.
_DELIBERATELY_INVALID_JSON_LINES: Final = frozenset(
    {"malformed/invalid-json-line.ndjson"}
)

_SECRET_PATTERNS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        r"AKIA[0-9A-Z]{16}",
        r"gh[pousr]_[A-Za-z0-9]{36,}",
        r"sk-[A-Za-z0-9]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
)


def _load_manifest() -> dict[str, object]:
    with MANIFEST_PATH.open("rb") as manifest_file:
        return cast(dict[str, object], json.load(manifest_file))


def _require_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _require_str(value: object) -> str:
    assert isinstance(value, str)
    return value


def _fixture_entries(manifest: dict[str, object]) -> tuple[dict[str, object], ...]:
    entries = manifest["fixtures"]
    assert isinstance(entries, list)
    return tuple(_require_dict(entry) for entry in cast(list[object], entries))


def _entry_path(entry: dict[str, object]) -> str:
    path = _require_str(entry["path"])
    assert path
    return path


def _all_fixture_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in FIXTURES_ROOT.rglob("*")
            if path.is_file() and path.name != "MANIFEST.json"
        )
    )


# --- version and status attribution -----------------------------------------


def test_manifest_declares_the_exact_candidate_version() -> None:
    manifest = _load_manifest()
    assert manifest["opencode_version"] == OPENCODE_VERSION
    assert manifest["compatibility_status"] == "candidate"


def test_manifest_records_verifiable_pack_wide_provenance() -> None:
    manifest = _load_manifest()
    provenance = _require_dict(manifest["provenance"])

    method = _require_str(provenance["method"])
    assert method.strip()

    references = provenance["references"]
    assert isinstance(references, list) and references
    for reference in cast(list[object], references):
        assert isinstance(reference, str) and reference.strip()


# --- inventory ---------------------------------------------------------------


def test_every_fixture_file_on_disk_is_listed_in_the_manifest_exactly_once() -> None:
    manifest = _load_manifest()
    manifest_paths = [_entry_path(entry) for entry in _fixture_entries(manifest)]
    assert len(manifest_paths) == len(set(manifest_paths))

    disk_paths = {str(path.relative_to(FIXTURES_ROOT)) for path in _all_fixture_files()}
    assert set(manifest_paths) == disk_paths


def test_manifest_covers_every_required_fixture_category() -> None:
    manifest = _load_manifest()
    categories = {
        _require_str(entry["category"]) for entry in _fixture_entries(manifest)
    }
    missing = _REQUIRED_CATEGORIES - categories
    assert not missing, (
        f"fixture pack is missing required categories: {sorted(missing)}"
    )


def test_every_fixture_entry_has_verifiable_attribution() -> None:
    manifest = _load_manifest()
    for entry in _fixture_entries(manifest):
        assert entry["kind"] in _VALID_KINDS
        assert entry["role"] is None or entry["role"] in _VALID_ROLES

        category = _require_str(entry["category"])
        assert category.strip()

        description = _require_str(entry["description"])
        assert description.strip()

        sha256 = _require_str(entry["sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", sha256)


def test_pack_contains_both_positive_and_negative_trust_boundary_cases() -> None:
    manifest = _load_manifest()
    kinds = {_require_str(entry["kind"]) for entry in _fixture_entries(manifest)}
    assert kinds == _VALID_KINDS

    provider_categories = {
        _require_str(entry["category"])
        for entry in _fixture_entries(manifest)
        if _require_str(entry["category"]).startswith("provider-")
    }
    assert provider_categories == {"provider-trusted", "provider-lookalike"}


# --- integrity -----------------------------------------------------------------


def test_every_fixture_digest_matches_its_manifest_entry() -> None:
    manifest = _load_manifest()
    for entry in _fixture_entries(manifest):
        path = FIXTURES_ROOT / _entry_path(entry)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], (
            f"{entry['path']} content does not match its recorded sha256; "
            "fixtures are immutable evidence and must not drift silently"
        )


def test_json_fixtures_under_debug_and_export_are_well_formed() -> None:
    for directory in ("debug", "export"):
        for path in sorted((FIXTURES_ROOT / directory).glob("*.json")):
            json.loads(path.read_text(encoding="utf-8"))


def test_ndjson_fixtures_are_line_delimited_json_unless_deliberately_malformed() -> (
    None
):
    for directory in ("run", "provider"):
        for path in sorted((FIXTURES_ROOT / directory).glob("*.ndjson")):
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)

    for path in sorted((FIXTURES_ROOT / "malformed").glob("*.ndjson")):
        relative = str(path.relative_to(FIXTURES_ROOT))
        lines = path.read_text(encoding="utf-8").splitlines()
        if relative in _DELIBERATELY_INVALID_JSON_LINES:
            with pytest.raises(json.JSONDecodeError):
                for line in lines:
                    json.loads(line)
        else:
            for line in lines:
                json.loads(line)


def test_non_utf8_fixture_is_actually_invalid_utf8() -> None:
    path = FIXTURES_ROOT / "malformed" / "non-utf8-bytes.bin"
    with pytest.raises(UnicodeDecodeError):
        path.read_bytes().decode("utf-8")


def test_agent_fallback_warning_fixture_mixes_a_non_json_line_into_the_stream() -> None:
    path = FIXTURES_ROOT / "malformed" / "agent-fallback-warning.stdout.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2

    non_json_line_count = 0
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            non_json_line_count += 1
    assert non_json_line_count >= 1


# --- offline determinism -------------------------------------------------------


def test_fixture_pack_loads_deterministically_offline() -> None:
    first = {
        str(path.relative_to(FIXTURES_ROOT)): path.read_bytes()
        for path in _all_fixture_files()
    }
    second = {
        str(path.relative_to(FIXTURES_ROOT)): path.read_bytes()
        for path in _all_fixture_files()
    }
    assert first == second
    assert _load_manifest() == _load_manifest()


# --- no secrets ------------------------------------------------------------------


def test_no_fixture_contains_an_obvious_secret_pattern() -> None:
    for path in (*_all_fixture_files(), MANIFEST_PATH):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # the deliberately invalid-UTF-8 fixture has no text to scan
        for pattern in _SECRET_PATTERNS:
            assert not pattern.search(text), (
                f"{path} matches secret pattern {pattern.pattern}"
            )


# --- compatibility documentation ------------------------------------------------


def test_compatibility_doc_declares_the_version_candidate_not_supported() -> None:
    doc = COMPATIBILITY_DOC_PATH.read_text(encoding="utf-8")
    assert OPENCODE_VERSION in doc
    assert "candidate" in doc.lower()
    assert "not yet supported" in doc.lower() or "not supported" in doc.lower()
