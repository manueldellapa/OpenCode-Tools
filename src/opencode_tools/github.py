"""Target-specific GitHub repository identity resolution (ADR-007; System
Design SS17.1).

This module proves, deterministically and without ever reading `cwd` or the
workspace, which `host/owner/repository` a target Git repository addresses.
The precedence, applied to the `GithubTargetOverride` (if any) whose
`workspace_relative` matches the target:

1. an override `repository` is the expected identity outright, unless the
   override also names a `remote`, in which case that remote's fetch URLs
   must agree with it (coherence check);
2. otherwise an override `remote` alone must resolve, by itself, to exactly
   one distinct GitHub identity among its accepted fetch URLs;
3. otherwise `origin` is preferred if it exists and its accepted fetch URLs
   agree on exactly one identity;
4. otherwise exactly one distinct GitHub identity must be derivable from
   every remote's accepted fetch URLs combined -- zero or several is
   `PreflightError`.

Accepted fetch URL forms are HTTPS, `ssh://`, and scp-like
(`user@host:owner/repo.git`); userinfo, query strings, and fragments are
always discarded and never persisted into a `RepositoryIdentity` or an
error. Host is lower-cased; a trailing slash and a trailing `.git` suffix
are stripped from the repository segment; owner/repository comparison is
ASCII case-insensitive while the returned identity keeps one validated
display spelling. `github.com` and any GitHub Enterprise host are accepted
equally here -- `gh` auth/host verification is a later work package.

This module does not read issue title or body, does not call any GitHub
API, and does not select a repository from `cwd` or the workspace. It
depends only on `domain`, `ports`, and `errors` -- never `git_safety` or
`opencode` -- and duplicates its own small, read-only probe-running
helpers in the same shape `git_safety.py` and `opencode.py` use for theirs,
rather than importing either module (System Design SS6's module table
keeps them independent).

M11-02 adds this module's other half (System Design SS17.2/SS17.3;
ADR-007; ADR-010): a bounded, fail-closed `gh` preflight -- `gh --version`
then `gh auth status --hostname <host>`, both read-only utility calls --
that must succeed before any `IssueLocator` can exist, and `locate_issue`,
which runs that preflight and only then pairs a resolved
`RepositoryIdentity` with a requested issue number. Neither this module nor
`locate_issue` ever fetches an issue's title or body, calls a native GitHub
API, or performs a second verifying fetch; `gh auth status`'s raw
stdout/stderr is never decoded, logged, or placed in any error -- only its
parsed `gh` version, resolved host, and a sanitized digest survive, in
`GhPreflightEvidence`, for a later milestone to log.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from opencode_tools.domain import (
    GithubTargetOverride,
    IssueLocator,
    ProcessResult,
    ProcessSpec,
    RepositoryIdentity,
    RunOutcome,
    TargetRepository,
)
from opencode_tools.errors import PreflightError
from opencode_tools.ports import AttemptLogSink, LogChannel, ProcessRunner

# Mirrors git_safety.py's own defensive stdout bound for one utility probe;
# not shared with it -- this module must not import git_safety (System
# Design SS6).
_UTILITY_OUTPUT_LIMIT_BYTES = 1_048_576

_ORIGIN_REMOTE_NAME = "origin"
_DEFAULT_GITHUB_HOST = "github.com"

_SOURCE_OVERRIDE = "override"
_SOURCE_ORIGIN = "origin"
_SOURCE_REMOTE = "remote"

# `[user@]host:owner/repo[.git]`, host has no `/`, `@`, or `:`, and the
# colon is never immediately followed by `//` -- that shape belongs to an
# already-scheme-prefixed URL (`ssh://...`, `http://...`) that the scp-like
# form must never also match.
_SCP_LIKE_PATTERN = re.compile(r"^(?:[^@/]+@)?(?P<host>[^/@:]+):(?!//)(?P<path>.+)$")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


@dataclass(frozen=True, slots=True)
class ParsedGithubUrl:
    """One fetch URL's normalized GitHub identity: display spelling only.

    `host` is already lower-cased; `owner`/`repository` keep the spelling
    the URL (or override slug) actually used -- callers compare identities
    case-insensitively via `.casefold()`, never by re-deriving a spelling.
    """

    host: str
    owner: str
    repository: str


@dataclass(frozen=True, slots=True)
class RemoteFetchUrl:
    """One `(remote name, fetch URL)` pair, in `git remote -v` output order."""

    remote_name: str
    url: str


def _split_owner_repository(path: str) -> tuple[str, str] | None:
    """Split a URL path or scp-like path segment into `(owner, repository)`.

    Strips a leading/trailing slash and a single trailing `.git` off the
    repository segment; returns `None` (never raises) for anything that is
    not exactly two non-empty segments once that stripping is done -- a
    non-GitHub-shaped path is simply not "accepted".
    """

    trimmed = path.strip("/")
    if not trimmed:
        return None
    segments = trimmed.split("/")
    if len(segments) != 2:
        return None
    owner, repository = segments
    if not owner or not repository:
        return None
    if repository != ".git" and repository.endswith(".git"):
        repository = repository[: -len(".git")]
    if not owner or not repository:
        return None
    if _contains_control_character(owner) or _contains_control_character(repository):
        return None
    return owner, repository


def parse_https_fetch_url(raw_url: str) -> ParsedGithubUrl | None:
    """Parse an HTTPS fetch URL, discarding userinfo, query, and fragment.

    Only the exact scheme `https` is accepted -- plain `http` is not
    (ADR-007). Returns `None`, never raises, for anything unparseable or
    not GitHub-shaped.
    """

    if _contains_control_character(raw_url):
        return None
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    host = parsed.hostname
    if not host or _contains_control_character(host):
        return None
    owner_repository = _split_owner_repository(parsed.path)
    if owner_repository is None:
        return None
    owner, repository = owner_repository
    return ParsedGithubUrl(host=host.lower(), owner=owner, repository=repository)


def parse_ssh_scheme_fetch_url(raw_url: str) -> ParsedGithubUrl | None:
    """Parse an `ssh://` fetch URL the same way `parse_https_fetch_url` does."""

    if _contains_control_character(raw_url):
        return None
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return None
    if parsed.scheme != "ssh":
        return None
    host = parsed.hostname
    if not host or _contains_control_character(host):
        return None
    owner_repository = _split_owner_repository(parsed.path)
    if owner_repository is None:
        return None
    owner, repository = owner_repository
    return ParsedGithubUrl(host=host.lower(), owner=owner, repository=repository)


def parse_scp_like_fetch_url(raw_url: str) -> ParsedGithubUrl | None:
    """Parse a scp-like fetch URL (`[user@]host:owner/repo[.git]`).

    There is no query, fragment, or port concept in this form; only
    userinfo is discarded.
    """

    if _contains_control_character(raw_url):
        return None
    match = _SCP_LIKE_PATTERN.fullmatch(raw_url)
    if match is None:
        return None
    host = match.group("host")
    if not host or _contains_control_character(host):
        return None
    owner_repository = _split_owner_repository(match.group("path"))
    if owner_repository is None:
        return None
    owner, repository = owner_repository
    return ParsedGithubUrl(host=host.lower(), owner=owner, repository=repository)


def parse_github_fetch_url(raw_url: str) -> ParsedGithubUrl | None:
    """Parse `raw_url` as HTTPS, then `ssh://`, then scp-like; else `None`.

    This is the single "accepted" test used everywhere in this module: a
    fetch URL that matches none of the three recognized forms, or whose
    path is not a clean two-segment `owner/repository`, is simply excluded
    from consideration rather than raising.
    """

    return (
        parse_https_fetch_url(raw_url)
        or parse_ssh_scheme_fetch_url(raw_url)
        or parse_scp_like_fetch_url(raw_url)
    )


_REMOTE_LINE_PATTERN = re.compile(
    r"^(?P<name>\S+)\t(?P<url>.+) \((?P<direction>fetch|push)\)$"
)


def parse_remote_v_fetch_urls(raw_output: str) -> tuple[RemoteFetchUrl, ...]:
    """Parse `git remote -v` output into its ordered fetch-only entries.

    Push-only lines are discarded entirely (ADR-007: only fetch URLs are
    ever consulted). A line that does not match the documented `<name>\\t
    <url> (fetch|push)` shape is skipped rather than raised on -- this
    parser only classifies already-successful `git remote -v` stdout, and
    every downstream ambiguity rule already fails closed on too few or too
    many accepted identities. Order is preserved exactly as printed, which
    is what the "first remote in output order" tie-break (SS17.1 point 5)
    depends on.
    """

    entries: list[RemoteFetchUrl] = []
    for line in raw_output.splitlines():
        match = _REMOTE_LINE_PATTERN.match(line)
        if match is None or match.group("direction") != "fetch":
            continue
        entries.append(
            RemoteFetchUrl(remote_name=match.group("name"), url=match.group("url"))
        )
    return tuple(entries)


def _identity_key(parsed: ParsedGithubUrl) -> tuple[str, str, str]:
    return (
        parsed.host.casefold(),
        parsed.owner.casefold(),
        parsed.repository.casefold(),
    )


def _accepted_identities(urls: tuple[str, ...]) -> tuple[ParsedGithubUrl, ...]:
    accepted: list[ParsedGithubUrl] = []
    for url in urls:
        parsed = parse_github_fetch_url(url)
        if parsed is not None:
            accepted.append(parsed)
    return tuple(accepted)


def _distinct_identities(
    parsed_urls: tuple[ParsedGithubUrl, ...],
) -> tuple[ParsedGithubUrl, ...]:
    """De-duplicate case-insensitively, keeping each identity's first
    encountered display spelling and never depending on set/dict hash
    iteration order (`dict` here only ever preserves insertion order,
    which is itself the deterministic `parsed_urls` order the caller
    already fixed -- mirrors `build_git_state_fingerprint`'s own sorted/
    stable-order discipline in `git_safety.py`, adapted to this data).
    """

    seen: dict[tuple[str, str, str], ParsedGithubUrl] = {}
    for parsed in parsed_urls:
        seen.setdefault(_identity_key(parsed), parsed)
    return tuple(seen.values())


def _distinct_identities_with_remote(
    entries: tuple[tuple[str, ParsedGithubUrl], ...],
) -> tuple[tuple[str, ParsedGithubUrl], ...]:
    seen: dict[tuple[str, str, str], tuple[str, ParsedGithubUrl]] = {}
    for remote_name, parsed in entries:
        seen.setdefault(_identity_key(parsed), (remote_name, parsed))
    return tuple(seen.values())


def _resolve_named_remote_identity(
    entries: tuple[RemoteFetchUrl, ...], remote_name: str
) -> ParsedGithubUrl:
    """Resolve a single distinct GitHub identity from one named remote's
    accepted fetch URLs (SS17.1 points 2a and 3 share this exact rule).

    Distinguishes, by error code, a remote that is not configured at all
    (`github.configured_remote_missing`) from one that is configured but
    whose accepted fetch URLs are zero or disagree
    (`github.remote_identity_ambiguous`) -- both are `PreflightError`, but
    a future reader/test can tell the two failure reasons apart.
    """

    if not any(entry.remote_name == remote_name for entry in entries):
        raise PreflightError(
            "github.configured_remote_missing",
            f"Remote {remote_name!r} is not configured in this repository.",
        )
    urls = tuple(entry.url for entry in entries if entry.remote_name == remote_name)
    accepted = _accepted_identities(urls)
    distinct = _distinct_identities(accepted)
    if len(distinct) != 1:
        raise PreflightError(
            "github.remote_identity_ambiguous",
            f"Remote {remote_name!r} has no single unambiguous GitHub "
            "identity among its accepted fetch URLs.",
            technical_detail=(
                f"remote={remote_name!r} accepted_url_count={len(accepted)} "
                f"distinct_identity_count={len(distinct)}"
            ),
        )
    return distinct[0]


def _parse_override_repository_slug(slug: str) -> ParsedGithubUrl:
    """Parse an already-validated `GithubTargetOverride.repository` slug.

    `owner/repo` or `host/owner/repo`, per `domain._is_repository_slug`;
    this is never a URL, so owner/repository are kept exactly as the
    override author wrote them -- only `host` is lower-cased, to satisfy
    `RepositoryIdentity`'s own invariant.
    """

    segments = slug.split("/")
    if len(segments) == 2:
        host, owner, repository = _DEFAULT_GITHUB_HOST, segments[0], segments[1]
    else:
        host, owner, repository = segments[0], segments[1], segments[2]
    return ParsedGithubUrl(host=host.lower(), owner=owner, repository=repository)


def _identity_from_parsed(
    parsed: ParsedGithubUrl, *, source: str, remote_name: str | None
) -> RepositoryIdentity:
    return RepositoryIdentity(
        host=parsed.host,
        owner=parsed.owner,
        repository=parsed.repository,
        source=source,
        remote_name=remote_name,
    )


def _find_override(
    github_targets: tuple[GithubTargetOverride, ...], workspace_relative: Path
) -> GithubTargetOverride | None:
    for override in github_targets:
        if override.workspace_relative == workspace_relative:
            return override
    return None


def resolve_repository_identity_from_remotes(
    *,
    target_workspace_relative: Path,
    github_targets: tuple[GithubTargetOverride, ...],
    remote_entries: tuple[RemoteFetchUrl, ...],
) -> RepositoryIdentity:
    """Pure precedence algorithm (System Design SS17.1), given an already
    parsed `git remote -v` fetch-URL listing -- no I/O, no `ProcessRunner`.

    Branch-to-error-code map (every failure is `PreflightError`):

    - override `repository` + override `remote`, remote missing/ambiguous
      -> `github.configured_remote_missing` / `github.remote_identity_ambiguous`;
    - override `repository` + override `remote`, remote resolves but
      disagrees with `repository` -> `github.override_remote_mismatch`;
    - override `repository` alone -> never fails (no remote is read);
    - override `remote` alone, missing/ambiguous ->
      `github.configured_remote_missing` / `github.remote_identity_ambiguous`;
    - no override, `origin` usable -> never fails (origin short-circuits);
    - no override, no usable `origin`, zero or multiple distinct identities
      across every remote's accepted fetch URLs -> `github.no_unique_identity`.

    `remote_entries` may be empty whenever the caller already knows no
    remote consultation is required (an override `repository` with no
    override `remote`) -- every other branch requires the caller to have
    actually read `git remote -v` first.
    """

    override = _find_override(github_targets, target_workspace_relative)

    if override is not None and override.repository is not None:
        expected = _parse_override_repository_slug(override.repository)
        if override.remote is not None:
            remote_identity = _resolve_named_remote_identity(
                remote_entries, override.remote
            )
            if _identity_key(remote_identity) != _identity_key(expected):
                raise PreflightError(
                    "github.override_remote_mismatch",
                    "The override repository and override remote disagree "
                    "on the resolved GitHub identity.",
                    technical_detail=(
                        f"override_repository={expected.host}/{expected.owner}/"
                        f"{expected.repository} remote_derived={remote_identity.host}/"
                        f"{remote_identity.owner}/{remote_identity.repository}"
                    ),
                )
            return _identity_from_parsed(
                expected, source=_SOURCE_OVERRIDE, remote_name=override.remote
            )
        return _identity_from_parsed(
            expected, source=_SOURCE_OVERRIDE, remote_name=None
        )

    if override is not None and override.remote is not None:
        remote_identity = _resolve_named_remote_identity(
            remote_entries, override.remote
        )
        return _identity_from_parsed(
            remote_identity, source=_SOURCE_REMOTE, remote_name=override.remote
        )

    if any(entry.remote_name == _ORIGIN_REMOTE_NAME for entry in remote_entries):
        origin_urls = tuple(
            entry.url
            for entry in remote_entries
            if entry.remote_name == _ORIGIN_REMOTE_NAME
        )
        origin_distinct = _distinct_identities(_accepted_identities(origin_urls))
        if len(origin_distinct) == 1:
            return _identity_from_parsed(
                origin_distinct[0],
                source=_SOURCE_ORIGIN,
                remote_name=_ORIGIN_REMOTE_NAME,
            )

    accepted_with_remote: list[tuple[str, ParsedGithubUrl]] = []
    for entry in remote_entries:
        parsed = parse_github_fetch_url(entry.url)
        if parsed is not None:
            accepted_with_remote.append((entry.remote_name, parsed))

    distinct_with_remote = _distinct_identities_with_remote(tuple(accepted_with_remote))
    if len(distinct_with_remote) != 1:
        identities_detail = ", ".join(
            f"{remote_name}:{parsed.host}/{parsed.owner}/{parsed.repository}"
            for remote_name, parsed in distinct_with_remote
        )
        raise PreflightError(
            "github.no_unique_identity",
            "No single unambiguous GitHub identity could be resolved among "
            "the target's remotes.",
            technical_detail=(
                f"distinct_identity_count={len(distinct_with_remote)}"
                + (f" identities=[{identities_detail}]" if identities_detail else "")
            ),
        )
    remote_name, identity = distinct_with_remote[0]
    return _identity_from_parsed(
        identity, source=_SOURCE_REMOTE, remote_name=remote_name
    )


class _CapturingSink:
    """An in-memory `AttemptLogSink` for one `git remote -v` call's stdout.

    Duplicated from `git_safety.py`'s own `_CapturingSink` in shape rather
    than imported: this module must not import `git_safety` (System Design
    SS6 module table), exactly why `opencode.py` and `git_safety.py` each
    already keep their own private copy instead of sharing one.
    """

    def __init__(self, *, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._overflowed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        del timestamp
        if channel != "stdout" or self._overflowed:
            return
        if len(self._buffer) + len(payload) > self._max_bytes:
            self._overflowed = True
            return
        self._buffer.extend(payload)

    def close(self) -> None:
        return None

    @property
    def overflowed_stdout(self) -> bool:
        return self._overflowed

    def stdout_bytes(self) -> bytes:
        return bytes(self._buffer)


def _requires_remote_probe(override: GithubTargetOverride | None) -> bool:
    """Whether `resolve_repository_identity` must read `git remote -v` at
    all: every branch except an override `repository` with no override
    `remote` (SS17.1 point 1 alone) consults at least one remote.
    """

    return override is None or override.remote is not None


def _run_utility(
    process_runner: ProcessRunner,
    *,
    executable: Path,
    argv_tail: tuple[str, ...],
    cwd: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
    sink: AttemptLogSink,
) -> ProcessResult:
    """Run one bounded, read-only utility probe and return its `ProcessResult`.

    Generalizes M11-01's original `git remote -v`-only invocation to an
    arbitrary executable/argv/cwd/sink, so the `git remote -v`, `gh
    --version`, and `gh auth status` probes below all share one small
    `ProcessSpec`-construction helper instead of each duplicating it. This
    is this module's own private helper -- `opencode.py`'s `_run_utility`
    is a different module's private helper for a different executable and
    is never imported here (System Design SS6's module table).
    """

    spec = ProcessSpec(
        argv=(str(executable), *argv_tail),
        cwd=cwd,
        stdin=None,
        timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    return process_runner.run(spec, sink=sink)


def _run_remote_v(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target_root: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> str:
    """Run the single read-only `git -C <target> remote -v` probe this
    module is ever allowed to construct, and return its decoded stdout.

    A timeout, non-zero exit, oversized, or non-UTF-8 output all fail
    closed with `PreflightError` -- an incomplete probe is never treated as
    "no remotes" (NFR-008).
    """

    sink = _CapturingSink(
        path=Path("git-remote-v.log"), max_bytes=_UTILITY_OUTPUT_LIMIT_BYTES
    )
    result = _run_utility(
        process_runner,
        executable=git_executable,
        argv_tail=("-C", str(target_root), "remote", "-v"),
        cwd=target_root,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        sink=sink,
    )
    if result.outcome is not RunOutcome.SUCCEEDED:
        raise PreflightError(
            "github.remote_probe_failed",
            "git remote -v did not complete successfully.",
            technical_detail=(
                f"outcome={result.outcome.value} return_code={result.return_code}"
            ),
        )
    if sink.overflowed_stdout:
        raise PreflightError(
            "github.remote_probe_failed",
            "git remote -v output exceeded the defensive size limit.",
        )
    try:
        return sink.stdout_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PreflightError(
            "github.remote_probe_failed",
            "git remote -v output was not valid UTF-8.",
        ) from None


def resolve_repository_identity(
    process_runner: ProcessRunner,
    *,
    git_executable: Path,
    target: TargetRepository,
    github_targets: tuple[GithubTargetOverride, ...],
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> RepositoryIdentity:
    """Resolve `target`'s GitHub identity from override/remote evidence only
    (ADR-007; System Design SS17.1) -- never from `cwd` or the workspace.

    `git_executable` is accepted as-is, exactly like every probe in
    `git_safety.py`: this module never calls `shutil.which` itself, so a
    future caller resolves it once (`git_safety.resolve_git_executable`)
    and passes the same `Path` to both modules. Reads `git remote -v` at
    most once, in the target, and applies every rule in
    `resolve_repository_identity_from_remotes` purely in-memory against
    that one parsed result.
    """

    override = _find_override(github_targets, target.workspace_relative)
    remote_entries: tuple[RemoteFetchUrl, ...] = ()
    if _requires_remote_probe(override):
        raw_output = _run_remote_v(
            process_runner,
            git_executable=git_executable,
            target_root=target.root,
            utility_timeout_seconds=utility_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )
        remote_entries = parse_remote_v_fetch_urls(raw_output)

    return resolve_repository_identity_from_remotes(
        target_workspace_relative=target.workspace_relative,
        github_targets=github_targets,
        remote_entries=remote_entries,
    )


# =============================================================================
# gh preflight (M11-02; System Design SS17.2/SS17.3; ADR-007; ADR-010)
# =============================================================================

# Both `gh` probes below are host/auth-level checks, never tied to any
# repository target, so they are spawned from a fixed, always-present POSIX
# directory rather than ever reading the caller's actual `cwd` -- the same
# invariant this module already keeps for identity resolution itself (this
# module's own docstring; ADR-007).
_HOST_LEVEL_PROBE_CWD = Path("/")

# `gh version 2.40.1 (2023-12-13)` -> `2.40.1`; matched only against the
# first line of `gh --version`'s output. Any parsable `digits.digits.digits`
# token is accepted -- unlike OpenCode's exact-match `1.17.18` policy
# (`opencode.check_version`), there is no pinned `gh` version.
_GH_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True, slots=True)
class GhPreflightEvidence:
    """Sanitized evidence from one successful `gh` preflight (SS17.2).

    Deliberately small and local to this module rather than added to
    `domain.py`: nothing downstream persists it yet in M11-02, it merely
    gives a later milestone something ready to log. `host` and `gh_version`
    are already-known-safe facts (the resolved host and the parsed version
    string); `auth_status_digest` is a SHA-256 digest -- never the raw
    `gh auth status` stdout/stderr this module never decodes or keeps.
    """

    host: str
    gh_version: str
    auth_status_digest: str


class _DiscardingSink:
    """An `AttemptLogSink` that discards every write immediately.

    Used only for `gh auth status`: System Design SS17.2 requires that its
    raw stdout/stderr is "non persistito" -- never persisted -- and this
    sink guarantees this module never even holds a transient in-memory copy
    of it (unlike `_CapturingSink`, there is no buffer to read back).
    `ProcessResult.outcome`/`return_code`/`stdout_sha256`/`stderr_sha256`
    (computed by `SubprocessRunner` directly off the raw bytes, independent
    of whatever sink is attached) remain the only surviving evidence of
    what the call produced.
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def write(self, channel: LogChannel, payload: bytes, timestamp: datetime) -> None:
        del channel, payload, timestamp

    def close(self) -> None:
        return None


def resolve_gh_executable() -> Path:
    """Resolve the `gh` executable once via `PATH`.

    Mirrors `git_safety.resolve_git_executable`/`opencode.resolve_executable`
    in shape; `gh` is a different executable than `git`, so this module
    resolves its own rather than importing either sibling. Never tries an
    alias or an automatic install; a missing executable fails closed
    immediately, before any `gh` call and before any `IssueLocator` can
    exist.
    """

    found = shutil.which("gh")
    if found is None:
        raise PreflightError(
            "github.gh_executable_not_found",
            "The 'gh' executable was not found on PATH.",
        )
    return Path(found).resolve()


def parse_gh_version(raw_output: str) -> str | None:
    """Extract the semver-shaped token from `gh --version`'s first line.

    `gh --version` is typically multi-line (a version line followed by a
    release URL); only the first line is ever inspected. Returns `None`,
    never raises, when no `digits.digits.digits` token is present there --
    the "version not parsable" preflight failure (SS17.2).
    """

    lines = raw_output.splitlines()
    first_line = lines[0] if lines else ""
    match = _GH_VERSION_PATTERN.search(first_line)
    if match is None:
        return None
    return match.group(0)


def _run_gh_version(
    process_runner: ProcessRunner,
    *,
    gh_executable: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> str:
    """Run `gh --version` and return its decoded stdout.

    Bounded and read-only, mirroring `_run_remote_v`'s own discipline: a
    timeout, non-zero exit, oversized, or non-UTF-8 capture all fail closed
    with `PreflightError` rather than being treated as "no version"
    (NFR-008). Unlike `gh auth status`, this call's own output is not the
    "raw auth output" SS17.2 forbids persisting -- it must be read to parse
    the version, and only the parsed token, never this raw text, survives
    into `GhPreflightEvidence`.
    """

    sink = _CapturingSink(
        path=Path("gh-version.log"), max_bytes=_UTILITY_OUTPUT_LIMIT_BYTES
    )
    result = _run_utility(
        process_runner,
        executable=gh_executable,
        argv_tail=("--version",),
        cwd=_HOST_LEVEL_PROBE_CWD,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        sink=sink,
    )
    if result.outcome is not RunOutcome.SUCCEEDED:
        raise PreflightError(
            "github.gh_version_probe_failed",
            "gh --version did not complete successfully.",
            technical_detail=(
                f"outcome={result.outcome.value} return_code={result.return_code}"
            ),
        )
    if sink.overflowed_stdout:
        raise PreflightError(
            "github.gh_version_probe_failed",
            "gh --version output exceeded the defensive size limit.",
        )
    try:
        return sink.stdout_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PreflightError(
            "github.gh_version_probe_failed",
            "gh --version output was not valid UTF-8.",
        ) from None


def _run_gh_auth_status(
    process_runner: ProcessRunner,
    *,
    gh_executable: Path,
    host: str,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> ProcessResult:
    """Run `gh auth status --hostname <host>` and return its raw
    `ProcessResult` only -- never its stdout/stderr text.

    `_DiscardingSink` guarantees this module never buffers a copy of what
    the call printed; `ProcessResult.outcome`/`return_code` alone is the
    fail-closed signal, exactly like `git_safety._require_probe_succeeded`
    and `opencode._require_process_succeeded` treat "not a clean success"
    as the single condition for their own bounded probes -- `gh`'s
    human-readable auth text (e.g. "not logged in") is never parsed for
    specific phrases.
    """

    sink = _DiscardingSink(path=Path("gh-auth-status.log"))
    return _run_utility(
        process_runner,
        executable=gh_executable,
        argv_tail=("auth", "status", "--hostname", host),
        cwd=_HOST_LEVEL_PROBE_CWD,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        sink=sink,
    )


def _compute_auth_status_digest(result: ProcessResult) -> str:
    """Compute a sanitized digest for one `gh auth status` call (SS17.2).

    Hashes only an already-known-safe fact set -- `ProcessResult`'s own
    `stdout_sha256`/`stderr_sha256` (SHA-256 digests `SubprocessRunner`
    derives directly from the raw bytes streamed off the child, independent
    of `_DiscardingSink`) and its `return_code` -- never the raw auth text
    itself, which this module never decodes. Mirrors
    `opencode.compute_control_plane_digest`'s `hashlib.sha256(...).hexdigest()`
    style, adapted to plain values instead of parsed JSON structures.
    """

    canonical = (
        f"stdout_sha256={result.stdout_sha256} "
        f"stderr_sha256={result.stderr_sha256} "
        f"return_code={result.return_code}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_gh_preflight(
    process_runner: ProcessRunner,
    *,
    gh_executable: Path,
    host: str,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> GhPreflightEvidence:
    """Run both bounded `gh` preflight probes (System Design SS17.2) and
    return sanitized evidence, or fail closed before any `IssueLocator` can
    exist.

    `gh --version` runs first; an unparsable version fails closed without
    ever attempting `gh auth status`. `gh auth status --hostname host` must
    then complete as a clean, confirmed, zero-exit success -- the only
    accepted "authenticated for this host" signal (ADR-010's cooperative,
    assumed-uncompromised local `gh`; `github.com` and any GitHub
    Enterprise host are treated identically here). Only `host`, the parsed
    `gh_version`, and a sanitized digest of the auth call survive into the
    returned `GhPreflightEvidence`; its raw stdout/stderr never does.
    """

    raw_version_output = _run_gh_version(
        process_runner,
        gh_executable=gh_executable,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    gh_version = parse_gh_version(raw_version_output)
    if gh_version is None:
        raise PreflightError(
            "github.gh_version_unparseable",
            "gh --version output did not contain a parsable version.",
        )

    auth_result = _run_gh_auth_status(
        process_runner,
        gh_executable=gh_executable,
        host=host,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    if auth_result.outcome is not RunOutcome.SUCCEEDED:
        raise PreflightError(
            "github.gh_auth_failed",
            f"gh auth status did not confirm authentication for host {host!r}.",
            technical_detail=(
                f"outcome={auth_result.outcome.value} "
                f"return_code={auth_result.return_code}"
            ),
        )

    return GhPreflightEvidence(
        host=host,
        gh_version=gh_version,
        auth_status_digest=_compute_auth_status_digest(auth_result),
    )


def locate_issue(
    repository_identity: RepositoryIdentity,
    issue_number: int,
    *,
    process_runner: ProcessRunner,
    gh_executable: Path,
    utility_timeout_seconds: float,
    termination_grace_seconds: float,
) -> IssueLocator:
    """Pair a resolved `RepositoryIdentity` with `issue_number`, but only
    after a fresh `gh` preflight against `repository_identity.host`
    succeeds (System Design SS17.2/SS17.3; ADR-007).

    No `IssueLocator` is ever constructed when the preflight fails --
    `run_gh_preflight` raises `PreflightError` first, before this function
    so much as looks at `issue_number`. Mirrors the eventual
    `ports.IssueResolver.locate_issue` shape as a free function, the same
    discipline `resolve_repository_identity` already follows for
    `IssueResolver.resolve_repository` -- not a class satisfying the full
    Protocol. `issue_number`'s own positivity is entirely
    `IssueLocator.__post_init__`'s concern: an invalid `issue_number` is
    this function's own caller's contract violation, not an external-world
    preflight fact, so its `ValueError` is left to propagate unmodified
    rather than being re-wrapped as a `PreflightError`.
    """

    run_gh_preflight(
        process_runner,
        gh_executable=gh_executable,
        host=repository_identity.host,
        utility_timeout_seconds=utility_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )
    return IssueLocator(repository_identity=repository_identity, number=issue_number)


__all__ = (
    "GhPreflightEvidence",
    "ParsedGithubUrl",
    "RemoteFetchUrl",
    "locate_issue",
    "parse_gh_version",
    "parse_github_fetch_url",
    "parse_https_fetch_url",
    "parse_remote_v_fetch_urls",
    "parse_scp_like_fetch_url",
    "parse_ssh_scheme_fetch_url",
    "resolve_gh_executable",
    "resolve_repository_identity",
    "resolve_repository_identity_from_remotes",
    "run_gh_preflight",
)
