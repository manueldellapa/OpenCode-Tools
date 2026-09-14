"""Unit tests for GitHub repository identity resolution and `gh` preflight
in `github.py` (M11-01, M11-02; ADR-007; System Design SS17.1/SS17.2).

Every test here is pure: no subprocess is spawned and no real Git
repository is touched. URL parsing and `git remote -v` output parsing are
tested directly against literal strings; the full precedence/ambiguity
decision logic is tested directly against `RemoteFetchUrl` tuples built in
memory, via `resolve_repository_identity_from_remotes` -- never through a
`ProcessRunner`. `parse_gh_version` and `GhPreflightEvidence`'s shape are
likewise pure. Component-level, real-process wiring for
`resolve_repository_identity`, `run_gh_preflight`, and `locate_issue` lives
in `tests/component/test_github_boundary.py`.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from opencode_tools.domain import GithubTargetOverride, RepositoryIdentity
from opencode_tools.errors import PreflightError
from opencode_tools.github import (
    GhPreflightEvidence,
    ParsedGithubUrl,
    RemoteFetchUrl,
    parse_gh_version,
    parse_github_fetch_url,
    parse_https_fetch_url,
    parse_remote_v_fetch_urls,
    parse_scp_like_fetch_url,
    parse_ssh_scheme_fetch_url,
    resolve_gh_executable,
    resolve_repository_identity_from_remotes,
)

# =============================================================================
# URL parsing: HTTPS
# =============================================================================


def test_parse_https_fetch_url_accepts_a_plain_github_com_url() -> None:
    parsed = parse_https_fetch_url("https://github.com/owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_https_fetch_url_strips_git_suffix_and_trailing_slash() -> None:
    assert parse_https_fetch_url("https://github.com/owner/repo") == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )
    assert parse_https_fetch_url("https://github.com/owner/repo/") == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )
    assert parse_https_fetch_url(
        "https://github.com/owner/repo.git/"
    ) == ParsedGithubUrl(host="github.com", owner="owner", repository="repo")


def test_parse_https_fetch_url_supports_enterprise_hosts() -> None:
    parsed = parse_https_fetch_url("https://ghe.example.com/owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="ghe.example.com", owner="owner", repository="repo"
    )


def test_parse_https_fetch_url_lower_cases_the_host_only() -> None:
    parsed = parse_https_fetch_url("https://GHE.Example.COM/Owner/Repo.git")
    assert parsed == ParsedGithubUrl(
        host="ghe.example.com", owner="Owner", repository="Repo"
    )


def test_parse_https_fetch_url_discards_userinfo_query_and_fragment() -> None:
    parsed = parse_https_fetch_url(
        "https://user:secret-token@github.com/owner/repo.git?x=1#section"
    )
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_https_fetch_url_ignores_a_port() -> None:
    parsed = parse_https_fetch_url("https://github.com:8443/owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_https_fetch_url_rejects_plain_http() -> None:
    assert parse_https_fetch_url("http://github.com/owner/repo.git") is None


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://github.com/owner",
        "https://github.com/",
        "https://github.com",
        "https://github.com/owner/repo/extra",
        "ftp://github.com/owner/repo.git",
        "not a url at all",
        "",
    ],
)
def test_parse_https_fetch_url_rejects_malformed_or_non_github_shaped_urls(
    raw_url: str,
) -> None:
    assert parse_https_fetch_url(raw_url) is None


# =============================================================================
# URL parsing: ssh://
# =============================================================================


def test_parse_ssh_scheme_fetch_url_accepts_a_plain_url() -> None:
    parsed = parse_ssh_scheme_fetch_url("ssh://git@github.com/owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_ssh_scheme_fetch_url_ignores_a_port_and_discards_userinfo() -> None:
    parsed = parse_ssh_scheme_fetch_url("ssh://git@github.com:22/owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_ssh_scheme_fetch_url_supports_enterprise_hosts() -> None:
    parsed = parse_ssh_scheme_fetch_url("ssh://git@ghe.example.com/owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="ghe.example.com", owner="owner", repository="repo"
    )


def test_parse_ssh_scheme_fetch_url_rejects_https() -> None:
    assert parse_ssh_scheme_fetch_url("https://github.com/owner/repo.git") is None


@pytest.mark.parametrize(
    "raw_url",
    [
        "ssh://github.com/owner",
        "ssh://github.com",
        "git@github.com:owner/repo.git",
        "not a url at all",
    ],
)
def test_parse_ssh_scheme_fetch_url_rejects_malformed_or_wrong_scheme(
    raw_url: str,
) -> None:
    assert parse_ssh_scheme_fetch_url(raw_url) is None


# =============================================================================
# URL parsing: scp-like
# =============================================================================


def test_parse_scp_like_fetch_url_accepts_the_canonical_form() -> None:
    parsed = parse_scp_like_fetch_url("git@github.com:owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_scp_like_fetch_url_accepts_without_a_git_suffix_or_userinfo() -> None:
    parsed = parse_scp_like_fetch_url("github.com:owner/repo")
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_scp_like_fetch_url_supports_enterprise_hosts() -> None:
    parsed = parse_scp_like_fetch_url("git@ghe.example.com:owner/repo.git")
    assert parsed == ParsedGithubUrl(
        host="ghe.example.com", owner="owner", repository="repo"
    )


def test_parse_scp_like_fetch_url_lower_cases_only_the_host() -> None:
    parsed = parse_scp_like_fetch_url("git@GHE.Example.COM:Owner/Repo.git")
    assert parsed == ParsedGithubUrl(
        host="ghe.example.com", owner="Owner", repository="Repo"
    )


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "http://github.com/owner/repo.git",
        "github.com:owner",
        "github.com:owner/repo/extra",
        "not a url at all",
        "",
    ],
)
def test_parse_scp_like_fetch_url_rejects_scheme_prefixed_or_malformed_urls(
    raw_url: str,
) -> None:
    assert parse_scp_like_fetch_url(raw_url) is None


# =============================================================================
# parse_github_fetch_url: dispatch across all three forms
# =============================================================================


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
    ],
)
def test_parse_github_fetch_url_accepts_every_recognized_form(raw_url: str) -> None:
    parsed = parse_github_fetch_url(raw_url)
    assert parsed == ParsedGithubUrl(
        host="github.com", owner="owner", repository="repo"
    )


def test_parse_github_fetch_url_rejects_garbage() -> None:
    assert parse_github_fetch_url("not a url, just garbage") is None
    assert parse_github_fetch_url("") is None


# =============================================================================
# git remote -v output parsing
# =============================================================================


def test_parse_remote_v_fetch_urls_parses_a_single_remote() -> None:
    raw = (
        "origin\thttps://github.com/owner/repo.git (fetch)\n"
        "origin\thttps://github.com/owner/repo.git (push)\n"
    )
    entries = parse_remote_v_fetch_urls(raw)
    assert entries == (
        RemoteFetchUrl(remote_name="origin", url="https://github.com/owner/repo.git"),
    )


def test_parse_remote_v_fetch_urls_parses_multiple_remotes_in_output_order() -> None:
    raw = (
        "origin\thttps://github.com/a/b.git (fetch)\n"
        "origin\thttps://github.com/a/b.git (push)\n"
        "upstream\thttps://github.com/c/d.git (fetch)\n"
        "upstream\thttps://github.com/c/d.git (push)\n"
    )
    entries = parse_remote_v_fetch_urls(raw)
    assert entries == (
        RemoteFetchUrl(remote_name="origin", url="https://github.com/a/b.git"),
        RemoteFetchUrl(remote_name="upstream", url="https://github.com/c/d.git"),
    )


def test_parse_remote_v_fetch_urls_keeps_two_disagreeing_fetch_urls_for_one_remote() -> (
    None
):
    raw = (
        "origin\thttps://github.com/a/b.git (fetch)\n"
        "origin\thttps://github.com/c/d.git (fetch)\n"
    )
    entries = parse_remote_v_fetch_urls(raw)
    assert entries == (
        RemoteFetchUrl(remote_name="origin", url="https://github.com/a/b.git"),
        RemoteFetchUrl(remote_name="origin", url="https://github.com/c/d.git"),
    )


def test_parse_remote_v_fetch_urls_keeps_two_agreeing_fetch_urls_for_one_remote() -> (
    None
):
    raw = (
        "origin\thttps://github.com/a/b.git (fetch)\n"
        "origin\tgit@github.com:a/b.git (fetch)\n"
    )
    entries = parse_remote_v_fetch_urls(raw)
    assert len(entries) == 2
    assert all(entry.remote_name == "origin" for entry in entries)


def test_parse_remote_v_fetch_urls_ignores_push_only_lines() -> None:
    raw = "origin\thttps://github.com/owner/repo.git (push)\n"
    assert parse_remote_v_fetch_urls(raw) == ()


def test_parse_remote_v_fetch_urls_handles_empty_output() -> None:
    assert parse_remote_v_fetch_urls("") == ()


def test_parse_remote_v_fetch_urls_skips_unrecognized_lines() -> None:
    raw = "garbage line with no tab or direction\n"
    assert parse_remote_v_fetch_urls(raw) == ()


# =============================================================================
# Precedence and ambiguity matrix (resolve_repository_identity_from_remotes)
# =============================================================================


def _override(
    workspace_relative: str = ".",
    *,
    remote: str | None = None,
    repository: str | None = None,
) -> GithubTargetOverride:
    return GithubTargetOverride(
        workspace_relative=Path(workspace_relative),
        remote=remote,
        repository=repository,
    )


def _fetch(remote_name: str, url: str) -> RemoteFetchUrl:
    return RemoteFetchUrl(remote_name=remote_name, url=url)


def test_override_repository_only_trusts_it_with_no_remote_consultation() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(_override(repository="Owner/Repo"),),
        remote_entries=(),
    )
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="Owner",
        repository="Repo",
        source="override",
        remote_name=None,
    )


def test_override_repository_with_three_segment_slug_uses_first_as_host() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(_override(repository="GHE.Example.COM/Owner/Repo"),),
        remote_entries=(),
    )
    assert identity.host == "ghe.example.com"
    assert identity.owner == "Owner"
    assert identity.repository == "Repo"


def test_override_repository_and_matching_remote_returns_override_spelling() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(_override(remote="upstream", repository="owner/repo"),),
        remote_entries=(_fetch("upstream", "https://github.com/OWNER/REPO.git"),),
    )
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="override",
        remote_name="upstream",
    )


def test_override_repository_and_mismatched_remote_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(_override(remote="upstream", repository="owner/repo"),),
            remote_entries=(_fetch("upstream", "https://github.com/other/other.git"),),
        )
    assert exc_info.value.code == "github.override_remote_mismatch"


def test_override_repository_and_remote_missing_from_repo_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(_override(remote="upstream", repository="owner/repo"),),
            remote_entries=(),
        )
    assert exc_info.value.code == "github.configured_remote_missing"


def test_override_repository_and_ambiguous_remote_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(_override(remote="upstream", repository="owner/repo"),),
            remote_entries=(
                _fetch("upstream", "https://github.com/owner/repo.git"),
                _fetch("upstream", "https://github.com/other/other.git"),
            ),
        )
    assert exc_info.value.code == "github.remote_identity_ambiguous"


def test_override_remote_only_with_one_valid_identity() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(_override(remote="upstream"),),
        remote_entries=(_fetch("upstream", "git@github.com:owner/repo.git"),),
    )
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="remote",
        remote_name="upstream",
    )


def test_override_remote_only_with_an_ambiguous_remote_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(_override(remote="upstream"),),
            remote_entries=(
                _fetch("upstream", "https://github.com/owner/repo.git"),
                _fetch("upstream", "https://github.com/other/other.git"),
            ),
        )
    assert exc_info.value.code == "github.remote_identity_ambiguous"


def test_override_remote_only_with_zero_accepted_urls_is_ambiguous() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(_override(remote="upstream"),),
            remote_entries=(_fetch("upstream", "not-a-github-url"),),
        )
    assert exc_info.value.code == "github.remote_identity_ambiguous"


def test_override_remote_naming_an_absent_remote_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(_override(remote="upstream"),),
            remote_entries=(_fetch("origin", "https://github.com/owner/repo.git"),),
        )
    assert exc_info.value.code == "github.configured_remote_missing"


def test_no_override_uses_a_usable_origin() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(),
        remote_entries=(_fetch("origin", "https://github.com/owner/repo.git"),),
    )
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="origin",
        remote_name="origin",
    )


def test_no_override_prefers_origin_over_a_second_unambiguous_remote() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(),
        remote_entries=(
            _fetch("origin", "https://github.com/owner/repo.git"),
            _fetch("upstream", "https://github.com/other/other.git"),
        ),
    )
    assert identity.source == "origin"
    assert identity.remote_name == "origin"
    assert (identity.owner, identity.repository) == ("owner", "repo")


def test_no_override_with_an_unusable_origin_falls_through_to_the_single_other_remote() -> (
    None
):
    """An origin with zero accepted URLs -- unusable, per SS17.1 point 4a's
    "accepted URLs disagree" clause -- must never itself raise; it only
    loses its "preferred" status and lets the wider scan pick the single
    remaining unambiguous identity (the issue's explicit acceptance
    criterion that an invalid origin must not prevail over a single valid
    alternative).
    """

    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(),
        remote_entries=(
            _fetch("origin", "not-a-github-url"),
            _fetch("upstream", "https://github.com/owner/repo.git"),
        ),
    )
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="owner",
        repository="repo",
        source="remote",
        remote_name="upstream",
    )


def test_no_override_origin_with_internally_disagreeing_urls_still_counts_toward_the_wider_pool() -> (
    None
):
    """Unlike a *zero-accepted* origin, an origin whose accepted URLs
    disagree with each other contributes those distinct identities to the
    wider SS17.1 point 4b scan rather than being excluded outright -- so
    two disagreeing origin identities plus a third, different remote is
    still an unresolved ambiguity, never a single winner.
    """

    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(),
            remote_entries=(
                _fetch("origin", "https://github.com/a/b.git"),
                _fetch("origin", "https://github.com/c/d.git"),
                _fetch("upstream", "https://github.com/e/f.git"),
            ),
        )
    assert exc_info.value.code == "github.no_unique_identity"


def test_no_override_with_completely_ambiguous_multi_remote_repo_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(),
            remote_entries=(
                _fetch("upstream1", "https://github.com/a/b.git"),
                _fetch("upstream2", "https://github.com/c/d.git"),
            ),
        )
    assert exc_info.value.code == "github.no_unique_identity"


def test_no_override_with_zero_remotes_fails_closed() -> None:
    with pytest.raises(PreflightError) as exc_info:
        resolve_repository_identity_from_remotes(
            target_workspace_relative=Path("."),
            github_targets=(),
            remote_entries=(),
        )
    assert exc_info.value.code == "github.no_unique_identity"


def test_case_insensitive_comparison_preserves_first_encountered_display_spelling() -> (
    None
):
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(),
        remote_entries=(
            _fetch("origin", "https://github.com/Owner/Repo.git"),
            _fetch("origin", "https://GITHUB.COM/OWNER/REPO.git"),
        ),
    )
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="Owner",
        repository="Repo",
        source="origin",
        remote_name="origin",
    )


def test_multiple_remote_names_mapping_to_one_identity_pick_the_first_in_output_order() -> (
    None
):
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("."),
        github_targets=(),
        remote_entries=(
            _fetch("mirror-a", "https://github.com/owner/repo.git"),
            _fetch("mirror-b", "git@github.com:owner/repo.git"),
        ),
    )
    assert identity.remote_name == "mirror-a"


def test_override_only_applies_to_the_matching_workspace_relative_target() -> None:
    identity = resolve_repository_identity_from_remotes(
        target_workspace_relative=Path("Backend"),
        github_targets=(_override(".", repository="wrong/wrong"),),
        remote_entries=(_fetch("origin", "https://github.com/right/right.git"),),
    )
    assert (identity.owner, identity.repository) == ("right", "right")
    assert identity.source == "origin"


# =============================================================================
# gh --version parsing (M11-02; System Design SS17.2)
# =============================================================================


def test_parse_gh_version_extracts_the_token_from_a_multi_line_banner() -> None:
    raw = (
        "gh version 2.40.1 (2023-12-13)\n"
        "https://github.com/cli/cli/releases/tag/v2.40.1\n"
    )
    assert parse_gh_version(raw) == "2.40.1"


def test_parse_gh_version_extracts_the_token_from_a_single_line() -> None:
    assert parse_gh_version("gh version 2.4.0\n") == "2.4.0"


def test_parse_gh_version_accepts_any_parsable_version_no_exact_pin() -> None:
    """Unlike OpenCode's exact-match `1.17.18` policy, any semver-shaped
    token is accepted -- there is no candidate `gh` version to pin against.
    """

    assert parse_gh_version("gh version 99.0.0\n") == "99.0.0"
    assert parse_gh_version("gh version 0.1.2\n") == "0.1.2"


def test_parse_gh_version_matches_the_leading_three_segments_of_a_longer_token() -> (
    None
):
    assert parse_gh_version("gh version 2.40.1.9000\n") == "2.40.1"


def test_parse_gh_version_ignores_leading_whitespace_on_the_first_line() -> None:
    assert parse_gh_version("   gh version 2.40.1\n") == "2.40.1"


def test_parse_gh_version_rejects_a_missing_patch_segment() -> None:
    assert parse_gh_version("gh version 2.40\n") is None


def test_parse_gh_version_rejects_garbled_output() -> None:
    assert parse_gh_version("command not found: gh\n") is None


def test_parse_gh_version_rejects_empty_output() -> None:
    assert parse_gh_version("") is None


def test_parse_gh_version_only_ever_inspects_the_first_line() -> None:
    """A version token on a later line must never be found -- SS17.2 fixes
    the parse to `gh --version`'s first line only.
    """

    raw = "gh cli, no version here\n2.40.1\n"
    assert parse_gh_version(raw) is None


# =============================================================================
# GhPreflightEvidence shape (M11-02; System Design SS17.2)
# =============================================================================


def test_gh_preflight_evidence_exposes_host_version_and_digest() -> None:
    evidence = GhPreflightEvidence(
        host="github.com", gh_version="2.40.1", auth_status_digest="a" * 64
    )
    assert evidence.host == "github.com"
    assert evidence.gh_version == "2.40.1"
    assert evidence.auth_status_digest == "a" * 64


def test_gh_preflight_evidence_supports_enterprise_hosts_too() -> None:
    evidence = GhPreflightEvidence(
        host="ghe.example.com", gh_version="2.40.1", auth_status_digest="b" * 64
    )
    assert evidence.host == "ghe.example.com"


def test_gh_preflight_evidence_equality_is_by_value() -> None:
    first = GhPreflightEvidence(
        host="github.com", gh_version="2.40.1", auth_status_digest="c" * 64
    )
    second = GhPreflightEvidence(
        host="github.com", gh_version="2.40.1", auth_status_digest="c" * 64
    )
    assert first == second


def test_gh_preflight_evidence_is_frozen() -> None:
    evidence = GhPreflightEvidence(
        host="github.com", gh_version="2.40.1", auth_status_digest="d" * 64
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.host = "attacker.example.com"  # type: ignore[misc]


# =============================================================================
# resolve_gh_executable (M11-02)
# =============================================================================


def test_resolve_gh_executable_returns_the_resolved_which_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(
        shutil, "which", lambda name: str(fake) if name == "gh" else None
    )
    assert resolve_gh_executable() == fake.resolve()


def test_resolve_gh_executable_fails_closed_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(PreflightError) as exc_info:
        resolve_gh_executable()
    assert exc_info.value.code == "github.gh_executable_not_found"
