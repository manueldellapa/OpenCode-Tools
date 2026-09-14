"""Unit tests for the `git-state-v1` content-sensitive fingerprint (M09-02)
and its bounded, fail-closed sampling primitives (M09-03).

Covers framing/hash/version, ordering independence, raw path handling,
regular/symlink/missing/other classification, the Git-significant
executable bit, index and untracked parsing, a second edit to an
already-`M` file, gitlink/submodule records, path-escape rejection, and
the non-raising branch/HEAD capture helpers M09-03 uses instead of the
M09-01 bootstrap gate's.

Everything here is either a pure function (`build_git_state_fingerprint`,
`parse_index_manifest`, `split_null_terminated_records`,
`classify_lstat_mode`, `capture_branch`, `capture_head`) or
`build_fingerprint_path_entry` against real files under `tmp_path` -- no
subprocess and no real Git repository is needed; end-to-end coverage
against a real repository, including race/deadline scenarios, lives in
`tests/component/test_git_repository.py`.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from opencode_tools.domain import GitState
from opencode_tools.errors import PreflightError
from opencode_tools.git_safety import (
    GIT_STATE_FINGERPRINT_VERSION,
    FingerprintGitlinkEntry,
    FingerprintPathEntry,
    build_fingerprint_path_entry,
    build_git_state_fingerprint,
    capture_branch,
    capture_head,
    classify_lstat_mode,
    parse_index_manifest,
    parse_porcelain_inventory,
    split_null_terminated_records,
)

# --- split_null_terminated_records ----------------------------------------


def test_split_null_terminated_records_handles_empty_input() -> None:
    assert split_null_terminated_records(b"") == ()


def test_split_null_terminated_records_splits_on_nul_and_drops_the_trailer() -> None:
    assert split_null_terminated_records(b"a\x00b\x00c\x00") == (b"a", b"b", b"c")


def test_split_null_terminated_records_preserves_raw_non_utf8_bytes() -> None:
    raw = b"caf\xe9.txt\x00"
    assert split_null_terminated_records(raw) == (b"caf\xe9.txt",)


# --- parse_index_manifest ---------------------------------------------------


def test_parse_index_manifest_parses_mode_object_id_stage_and_path() -> None:
    raw = b"100644 " + b"a" * 40 + b" 0\tfile.txt\x00"
    entries = parse_index_manifest(raw)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.mode == "100644"
    assert entry.object_id == "a" * 40
    assert entry.stage == 0
    assert entry.path == b"file.txt"


def test_parse_index_manifest_recognizes_a_gitlink_mode() -> None:
    raw = b"160000 " + b"b" * 40 + b" 0\tsubmodule\x00"
    entries = parse_index_manifest(raw)
    assert entries[0].mode == "160000"


def test_parse_index_manifest_handles_multiple_records_in_order() -> None:
    raw = (
        b"100644 " + b"a" * 40 + b" 0\ta.txt\x00100755 " + b"b" * 40 + b" 0\tb.txt\x00"
    )
    entries = parse_index_manifest(raw)
    assert [entry.path for entry in entries] == [b"a.txt", b"b.txt"]
    assert entries[1].mode == "100755"


def test_parse_index_manifest_preserves_a_tab_byte_within_the_path() -> None:
    raw = b"100644 " + b"a" * 40 + b" 0\tweird\tname.txt\x00"
    entries = parse_index_manifest(raw)
    assert entries[0].path == b"weird\tname.txt"


def test_parse_index_manifest_rejects_a_record_with_no_path() -> None:
    with pytest.raises(PreflightError) as exc_info:
        parse_index_manifest(b"100644 " + b"a" * 40 + b" 0\x00")
    assert exc_info.value.code == "git_safety.fingerprint_index_malformed"


def test_parse_index_manifest_rejects_a_non_numeric_stage() -> None:
    with pytest.raises(PreflightError) as exc_info:
        parse_index_manifest(b"100644 " + b"a" * 40 + b" x\tfile.txt\x00")
    assert exc_info.value.code == "git_safety.fingerprint_index_malformed"


# --- classify_lstat_mode -----------------------------------------------------


def test_classify_lstat_mode_identifies_a_regular_file() -> None:
    assert classify_lstat_mode(stat.S_IFREG | 0o644) == "regular"


def test_classify_lstat_mode_identifies_a_symlink() -> None:
    assert classify_lstat_mode(stat.S_IFLNK | 0o777) == "symlink"


def test_classify_lstat_mode_treats_a_fifo_as_other() -> None:
    assert classify_lstat_mode(stat.S_IFIFO | 0o644) == "other"


def test_classify_lstat_mode_treats_a_directory_as_other() -> None:
    assert classify_lstat_mode(stat.S_IFDIR | 0o755) == "other"


# --- build_fingerprint_path_entry -------------------------------------------


def test_build_fingerprint_path_entry_hashes_regular_file_content(
    tmp_path: Path,
) -> None:
    (tmp_path / "file.txt").write_bytes(b"hello\n")

    entry = build_fingerprint_path_entry(tmp_path, b"file.txt")

    assert entry.type == "regular"
    assert entry.executable is False
    assert entry.content_hash == hashlib.sha256(b"hello\n").digest()


def test_build_fingerprint_path_entry_detects_the_executable_bit(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "run.sh"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)

    entry = build_fingerprint_path_entry(tmp_path, b"run.sh")

    assert entry.executable is True


def test_build_fingerprint_path_entry_hashes_the_symlink_target(
    tmp_path: Path,
) -> None:
    (tmp_path / "real.txt").write_bytes(b"data\n")
    link = tmp_path / "link.txt"
    link.symlink_to("real.txt")

    entry = build_fingerprint_path_entry(tmp_path, b"link.txt")

    assert entry.type == "symlink"
    assert entry.content_hash == hashlib.sha256(b"real.txt").digest()


def test_build_fingerprint_path_entry_classifies_a_missing_path(
    tmp_path: Path,
) -> None:
    entry = build_fingerprint_path_entry(tmp_path, b"does-not-exist.txt")

    assert entry.type == "missing"
    assert entry.content_hash is None


def test_build_fingerprint_path_entry_classifies_a_fifo_as_other(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    entry = build_fingerprint_path_entry(tmp_path, b"pipe")

    assert entry.type == "other"
    assert entry.content_hash is None


def test_build_fingerprint_path_entry_raises_on_an_unreadable_file(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "secret.txt"
    unreadable.write_bytes(b"top secret\n")
    unreadable.chmod(0o000)
    try:
        with pytest.raises(PreflightError) as exc_info:
            build_fingerprint_path_entry(tmp_path, b"secret.txt")
        assert exc_info.value.code == "git_safety.fingerprint_path_unreadable"
    finally:
        unreadable.chmod(0o644)


def test_build_fingerprint_path_entry_rejects_a_path_escaping_the_target(
    tmp_path: Path,
) -> None:
    with pytest.raises(PreflightError) as exc_info:
        build_fingerprint_path_entry(tmp_path, b"../outside.txt")
    assert exc_info.value.code == "git_safety.fingerprint_path_escapes_target"


# --- capture_branch / capture_head (M09-03's non-raising captures) ---------


def test_capture_branch_returns_the_branch_name() -> None:
    assert capture_branch("main\n") == "main"


def test_capture_branch_returns_none_for_a_detached_head() -> None:
    assert capture_branch("") is None


def test_capture_head_returns_the_stripped_sha() -> None:
    sha = "a" * 40
    assert capture_head(f"{sha}\n") == sha


def test_capture_head_raises_on_empty_output() -> None:
    with pytest.raises(PreflightError) as exc_info:
        capture_head("")
    assert exc_info.value.code == "git_safety.state_head_probe_failed"


# --- parse_porcelain_inventory (M09-05's change inventory) ------------------


def test_parse_porcelain_inventory_handles_empty_output() -> None:
    assert parse_porcelain_inventory(b"") == ((), (), ())


def test_parse_porcelain_inventory_classifies_untracked() -> None:
    staged, unstaged, untracked = parse_porcelain_inventory(b"?? new.txt\x00")
    assert staged == ()
    assert unstaged == ()
    assert untracked == ("new.txt",)


def test_parse_porcelain_inventory_classifies_a_staged_modification() -> None:
    staged, unstaged, untracked = parse_porcelain_inventory(b"M  staged.txt\x00")
    assert staged == ("staged.txt",)
    assert unstaged == ()
    assert untracked == ()


def test_parse_porcelain_inventory_classifies_an_unstaged_modification() -> None:
    staged, unstaged, untracked = parse_porcelain_inventory(b" M unstaged.txt\x00")
    assert staged == ()
    assert unstaged == ("unstaged.txt",)
    assert untracked == ()


def test_parse_porcelain_inventory_reports_both_columns_for_mm() -> None:
    staged, unstaged, untracked = parse_porcelain_inventory(b"MM both.txt\x00")
    assert staged == ("both.txt",)
    assert unstaged == ("both.txt",)
    assert untracked == ()


def test_parse_porcelain_inventory_distinguishes_all_three_categories() -> None:
    raw = b"M  staged.txt\x00 M unstaged.txt\x00?? new.txt\x00"
    staged, unstaged, untracked = parse_porcelain_inventory(raw)
    assert staged == ("staged.txt",)
    assert unstaged == ("unstaged.txt",)
    assert untracked == ("new.txt",)


def test_parse_porcelain_inventory_consumes_the_orig_path_of_a_rename() -> None:
    raw = b"R  renamed.txt\x00file.txt\x00?? new.txt\x00"
    staged, unstaged, untracked = parse_porcelain_inventory(raw)
    assert staged == ("renamed.txt",)
    assert unstaged == ()
    assert untracked == ("new.txt",)


def test_parse_porcelain_inventory_is_display_safe_for_non_utf8_paths() -> None:
    raw = b"?? caf\xe9.txt\x00"
    staged, unstaged, untracked = parse_porcelain_inventory(raw)
    assert untracked == ("caf\\xe9.txt",)
    assert staged == ()
    assert unstaged == ()


# --- build_git_state_fingerprint: framing, hash, version, ordering ---------


def _fingerprint(
    *,
    porcelain_raw: bytes = b"",
    index_manifest_raw: bytes = b"",
    tracked_raw: bytes = b"",
    untracked_raw: bytes = b"",
    path_entries: tuple[FingerprintPathEntry, ...] = (),
    gitlink_entries: tuple[FingerprintGitlinkEntry, ...] = (),
) -> str:
    return build_git_state_fingerprint(
        porcelain_raw=porcelain_raw,
        index_manifest_raw=index_manifest_raw,
        tracked_raw=tracked_raw,
        untracked_raw=untracked_raw,
        path_entries=path_entries,
        gitlink_entries=gitlink_entries,
    )


def test_build_git_state_fingerprint_is_deterministic_for_identical_input() -> None:
    assert _fingerprint() == _fingerprint()


def test_build_git_state_fingerprint_returns_a_sha256_hex_digest() -> None:
    digest = _fingerprint()
    assert len(digest) == 64
    bytes.fromhex(digest)


def test_build_git_state_fingerprint_changes_with_porcelain_content() -> None:
    a = _fingerprint()
    b = _fingerprint(porcelain_raw=b" M file.txt\x00")
    assert a != b


def test_build_git_state_fingerprint_changes_with_index_manifest_content() -> None:
    a = _fingerprint()
    b = _fingerprint(index_manifest_raw=b"100644 " + b"a" * 40 + b" 0\tfile.txt\x00")
    assert a != b


def test_build_git_state_fingerprint_changes_with_untracked_list() -> None:
    a = _fingerprint()
    b = _fingerprint(untracked_raw=b"new.txt\x00")
    assert a != b


def test_build_git_state_fingerprint_is_order_independent_across_path_entries() -> None:
    entry_a = FingerprintPathEntry(
        path=b"a.txt", type="regular", executable=False, content_hash=b"\x01" * 32
    )
    entry_b = FingerprintPathEntry(
        path=b"b.txt", type="regular", executable=False, content_hash=b"\x02" * 32
    )

    assert _fingerprint(path_entries=(entry_a, entry_b)) == _fingerprint(
        path_entries=(entry_b, entry_a)
    )


def test_build_git_state_fingerprint_distinguishes_raw_paths() -> None:
    entry_a = FingerprintPathEntry(
        path=b"a.txt", type="regular", executable=False, content_hash=b"\x00" * 32
    )
    entry_b = FingerprintPathEntry(
        path=b"caf\xe9.txt",
        type="regular",
        executable=False,
        content_hash=b"\x00" * 32,
    )

    assert _fingerprint(path_entries=(entry_a,)) != _fingerprint(
        path_entries=(entry_b,)
    )


def test_build_git_state_fingerprint_changes_when_type_changes() -> None:
    base = FingerprintPathEntry(
        path=b"a.txt", type="regular", executable=False, content_hash=b"\x03" * 32
    )
    changed = replace(base, type="symlink")

    assert _fingerprint(path_entries=(base,)) != _fingerprint(path_entries=(changed,))


def test_build_git_state_fingerprint_changes_when_executable_bit_changes() -> None:
    base = FingerprintPathEntry(
        path=b"a.txt", type="regular", executable=False, content_hash=b"\x03" * 32
    )
    changed = replace(base, executable=True)

    assert _fingerprint(path_entries=(base,)) != _fingerprint(path_entries=(changed,))


def test_build_git_state_fingerprint_changes_with_content_hash() -> None:
    entry_a = FingerprintPathEntry(
        path=b"a.txt", type="regular", executable=False, content_hash=b"\x01" * 32
    )
    entry_b = FingerprintPathEntry(
        path=b"a.txt", type="regular", executable=False, content_hash=b"\x02" * 32
    )

    assert _fingerprint(path_entries=(entry_a,)) != _fingerprint(
        path_entries=(entry_b,)
    )


def test_build_git_state_fingerprint_detects_a_second_edit_to_an_already_m_file() -> (
    None
):
    """A second edit to an already-`M` file must change the digest even
    though the porcelain summary line (`" M file.txt"`) does not (the PRD's
    own motivation for `git-state-v1`, System Design SS11.2).
    """

    same_porcelain = b" M file.txt\x00"
    same_index = b"100644 " + b"a" * 40 + b" 0\tfile.txt\x00"

    first_edit = FingerprintPathEntry(
        path=b"file.txt",
        type="regular",
        executable=False,
        content_hash=hashlib.sha256(b"first edit\n").digest(),
    )
    second_edit = FingerprintPathEntry(
        path=b"file.txt",
        type="regular",
        executable=False,
        content_hash=hashlib.sha256(b"second edit\n").digest(),
    )

    first_digest = _fingerprint(
        porcelain_raw=same_porcelain,
        index_manifest_raw=same_index,
        tracked_raw=b"file.txt\x00",
        path_entries=(first_edit,),
    )
    second_digest = _fingerprint(
        porcelain_raw=same_porcelain,
        index_manifest_raw=same_index,
        tracked_raw=b"file.txt\x00",
        path_entries=(second_edit,),
    )

    assert first_digest != second_digest


def test_build_git_state_fingerprint_changes_with_a_gitlink_object_id() -> None:
    a = _fingerprint(
        gitlink_entries=(FingerprintGitlinkEntry(path=b"sub", object_id="a" * 40),)
    )
    b = _fingerprint(
        gitlink_entries=(FingerprintGitlinkEntry(path=b"sub", object_id="b" * 40),)
    )

    assert a != b


def test_build_git_state_fingerprint_is_order_independent_across_gitlinks() -> None:
    link_a = FingerprintGitlinkEntry(path=b"a-sub", object_id="a" * 40)
    link_b = FingerprintGitlinkEntry(path=b"b-sub", object_id="b" * 40)

    assert _fingerprint(gitlink_entries=(link_a, link_b)) == _fingerprint(
        gitlink_entries=(link_b, link_a)
    )


def test_git_state_fingerprint_version_constant_matches_domain_default() -> None:
    assert (
        GIT_STATE_FINGERPRINT_VERSION
        == GitState.__dataclass_fields__["fingerprint_version"].default
    )


# --- M09-06 [GATE BLOCCANTE M09]: scalability qualification evidence -------

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCALABILITY_DOC_PATH: Final = REPO_ROOT / "docs" / "git-state-v1-scalability.md"


def test_scalability_doc_records_a_pass_with_the_required_evidence_fields() -> None:
    doc = SCALABILITY_DOC_PATH.read_text(encoding="utf-8")
    assert "**PASS**" in doc
    # Corpus description (non-sensitive).
    assert "5,000" in doc
    assert "synthetic" in doc.lower()
    # OS, Python, and Git versions.
    assert "macOS" in doc
    assert "3.13.15" in doc
    assert "2.54.0" in doc
    # utility_timeout_seconds, repetition count, and observed durations.
    assert "utility_timeout_seconds" in doc
    assert "30.0" in doc
    assert "Repetitions" in doc
    assert "Duration (s)" in doc
    # Fail-closed behavior and the no-fallback constraint.
    assert "INDETERMINATE" in doc
    assert "porcelain-only" in doc
