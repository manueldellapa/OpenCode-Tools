"""Run ID, directory layout, private naming, `run.json` v1 serialization, and
its atomic, fail-closed persistence.

Covers the M08-01 through M08-03 slices of the runtime store (System Design
SS15.1-SS15.4, SS15.6; ADR-008; ADR-009): the UTC-plus-random run ID format,
the `<runtime_root>/runs/<run-id>` layout created via exclusive `mkdir` with
bounded collision retry, the canonical role/cycle/attempt attempt-log
filename, the exclusive, non-truncating, anti-symlink primitive used to open
a fresh artifact file, the deterministic UTF-8 encoding of a `RunRecord`
into `run.json` v1 bytes, and replacing `run.json` with that encoding via an
exclusive same-directory temp file, `flush`/`fsync`, and `os.replace()`. The
schema itself -- every group and canonical field System Design SS15.3 lists
-- is `domain.RunRecord` and `domain.to_primitive()` (M02). The attempt-log
sink itself (M08-04) is a later milestone and stays out of this module for
now, as does the runtime root's own ignore/ownership preflight, which is
`M10`'s bootstrap and is assumed already valid here.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from opencode_tools.domain import AgentRole, RunRecord, to_primitive
from opencode_tools.errors import LoggingError
from opencode_tools.ports import Clock

DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600

RUN_ID_PATTERN: Final = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}$")

_RUN_DIRECTORY_COLLISION_CODE: Final = "runlog.run_directory_collision"
_RANDOM_SUFFIX_BYTES: Final = 6
_DEFAULT_MAX_DIRECTORY_ATTEMPTS: Final = 8
_HEX_SUFFIX_PATTERN: Final = re.compile(r"^[0-9a-f]{12}$")


def _require_absolute_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a Path")
    if not value.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_valid_run_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("run_id must be a string")
    if not RUN_ID_PATTERN.fullmatch(value):
        raise ValueError("run_id must match the canonical run ID format")
    return value


def format_run_id(moment: datetime, suffix: str) -> str:
    """Return `<UTC compact microseconds>Z-<12 hex>` for `moment`/`suffix`.

    `moment` must already be a timezone-aware UTC `datetime`; `suffix` must
    already be exactly 12 lowercase hex characters (System Design SS15.2).
    """

    if not isinstance(moment, datetime):
        raise TypeError("moment must be a datetime")
    if moment.utcoffset() != timedelta(0):
        raise ValueError("moment must be timezone-aware UTC")
    if not isinstance(suffix, str) or not _HEX_SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError("suffix must be exactly 12 lowercase hex characters")

    return f"{moment.strftime('%Y%m%dT%H%M%S.%f')}Z-{suffix}"


def generate_run_id(clock: Clock) -> str:
    """Return a fresh run ID: `clock.now()` plus 12 cryptographically random
    hex characters (System Design SS15.2). Never touches the filesystem."""

    return format_run_id(clock.now(), secrets.token_hex(_RANDOM_SUFFIX_BYTES))


def _verify_private_directory(path: Path) -> None:
    """Fail closed unless `path` is a real, non-symlink directory whose mode
    does not exceed `DIRECTORY_MODE` (ADR-008 SS15.6): a defense-in-depth
    re-check against both a swapped-in symlink and a pre-existing directory
    inherited with looser permissions. Mode or ownership are never repaired
    automatically."""

    try:
        info = os.lstat(path)
    except OSError as error:
        raise LoggingError(
            "runlog.directory_unverifiable",
            f"could not verify runtime directory: {path}",
            technical_detail=type(error).__name__,
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise LoggingError(
            "runlog.directory_is_symlink",
            f"refusing a symlinked runtime directory: {path}",
        )
    if not stat.S_ISDIR(info.st_mode):
        raise LoggingError(
            "runlog.path_not_a_directory",
            f"expected a directory but found something else: {path}",
        )
    if stat.S_IMODE(info.st_mode) & ~DIRECTORY_MODE:
        raise LoggingError(
            "runlog.directory_mode_too_permissive",
            f"directory mode exceeds {oct(DIRECTORY_MODE)}: {path}",
        )


def _ensure_runs_root(runtime_root: Path) -> Path:
    runs_root = runtime_root / "runs"
    try:
        os.mkdir(runs_root, DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError as error:
        raise LoggingError(
            "runlog.directory_create_failed",
            f"failed to create runtime directory: {runs_root}",
            technical_detail=type(error).__name__,
        ) from None
    _verify_private_directory(runs_root)
    return runs_root


def create_run_directory(runtime_root: Path, run_id: str) -> Path:
    """Create `<runtime_root>/runs/<run_id>` via exclusive `mkdir`.

    `runs/` is created (mode `0700`) if missing. A pre-existing `run_id`
    directory raises `LoggingError` (code `runlog.run_directory_collision`)
    rather than being reused, truncated, or overwritten; callers wanting
    bounded regeneration on collision use `allocate_run_directory`.
    """

    _require_absolute_path(runtime_root, "runtime_root")
    _require_valid_run_id(run_id)

    runs_root = _ensure_runs_root(runtime_root)
    run_directory = runs_root / run_id
    try:
        os.mkdir(run_directory, DIRECTORY_MODE)
    except FileExistsError:
        raise LoggingError(
            _RUN_DIRECTORY_COLLISION_CODE,
            f"a run directory already exists: {run_directory}",
        ) from None
    except OSError as error:
        raise LoggingError(
            "runlog.run_directory_create_failed",
            f"failed to create run directory: {run_directory}",
            technical_detail=type(error).__name__,
        ) from None

    _verify_private_directory(run_directory)
    return run_directory


def allocate_run_directory(
    runtime_root: Path,
    clock: Clock,
    *,
    max_attempts: int = _DEFAULT_MAX_DIRECTORY_ATTEMPTS,
) -> tuple[str, Path]:
    """Generate a run ID and create its directory, retrying on collision.

    Each attempt draws a fresh `generate_run_id(clock)`; only the specific
    `runlog.run_directory_collision` failure is retried, bounded by
    `max_attempts` (System Design SS15.2: "rigenerazione bounded in caso di
    collisione"). Any other failure from `create_run_directory` (a
    structural problem, not a name clash) propagates immediately.
    """

    _require_absolute_path(runtime_root, "runtime_root")
    _require_positive_int(max_attempts, "max_attempts")

    last_collision: LoggingError | None = None
    for _ in range(max_attempts):
        run_id = generate_run_id(clock)
        try:
            return run_id, create_run_directory(runtime_root, run_id)
        except LoggingError as error:
            if error.code != _RUN_DIRECTORY_COLLISION_CODE:
                raise
            last_collision = error

    raise LoggingError(
        "runlog.run_directory_collision_exhausted",
        f"failed to allocate a unique run directory after {max_attempts} attempts",
        causes=(last_collision,) if last_collision is not None else (),
    )


def attempt_log_filename(
    role: AgentRole,
    review_cycle: int | None,
    provider_attempt: int,
) -> str:
    """Return the canonical attempt-log filename for one provider attempt.

    Encodes role, review cycle (when applicable), and provider attempt so
    every retry and review cycle produces a distinct, self-describing name
    (System Design SS15.2; FR-043; AC-021):
    `architect-provider-attempt-1.log`, `coder-cycle-1-provider-attempt-1.log`,
    `reviewer-cycle-1-provider-attempt-2.log`. Cycle nullability mirrors
    `AgentResult`: forbidden for the architect, required for coder/reviewer.
    """

    if type(role) is not AgentRole:
        raise TypeError("role must be AgentRole")
    if review_cycle is not None:
        _require_positive_int(review_cycle, "review_cycle")
    _require_positive_int(provider_attempt, "provider_attempt")

    if role is AgentRole.ARCHITECT:
        if review_cycle is not None:
            raise ValueError("architect review_cycle must be None")
        cycle_segment = ""
    else:
        if review_cycle is None:
            raise ValueError("coder and reviewer require a review_cycle")
        cycle_segment = f"-cycle-{review_cycle}"

    role_name = role.value.lower()
    return f"{role_name}{cycle_segment}-provider-attempt-{provider_attempt}.log"


def open_private_exclusive(path: Path) -> int:
    """Open a brand-new, private file descriptor at `path`.

    Uses `O_CREAT | O_EXCL | O_NOFOLLOW` so a pre-existing file, directory,
    or symlink at `path` always fails closed instead of being silently
    reused, truncated, or followed (System Design SS15.2, SS15.6; ADR-008;
    ADR-009). Returns a raw file descriptor, mode `0600`, that the caller
    owns and must close.
    """

    _require_absolute_path(path, "path")

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        return os.open(path, flags, FILE_MODE)
    except FileExistsError:
        raise LoggingError(
            "runlog.artifact_file_collision",
            f"refusing to overwrite an existing artifact: {path}",
        ) from None
    except OSError as error:
        raise LoggingError(
            "runlog.artifact_file_open_failed",
            f"failed to open artifact file: {path}",
            technical_detail=type(error).__name__,
        ) from None


def serialize_run_record(record: RunRecord) -> bytes:
    """Serialize `record` into canonical `run.json` v1 bytes.

    The schema -- every group System Design SS15.3 lists (input, config,
    environment, timing, state, git, timeline, attempts, errors,
    persistence, result) and their canonical minimum fields -- is already
    fully represented by `RunRecord` and `to_primitive()`; this only fixes
    the on-disk *text*: UTF-8, `NaN`/`Infinity` forbidden, the field order
    `to_primitive()` already produces (dataclass declaration order, dict
    insertion order -- never re-sorted, so serializing the same `record`
    twice is byte-identical), and exactly one trailing newline. This never
    touches the filesystem; atomic replace and failure semantics belong to
    `M08-03`.
    """

    if type(record) is not RunRecord:
        raise TypeError("record must be RunRecord")

    primitive = to_primitive(record)
    text = json.dumps(primitive, allow_nan=False, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


_TEMP_DOCUMENT_SUFFIX: Final = ".tmp"


def _temp_document_name() -> str:
    return f".run.json.{secrets.token_hex(8)}{_TEMP_DOCUMENT_SUFFIX}"


def _best_effort_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_directory_best_effort(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def persist_run_record(record: RunRecord) -> None:
    """Atomically replace `record.artifact_path` (`run.json`) on disk.

    Follows System Design SS15.4 exactly: serialize fully in memory first
    (`serialize_run_record`), write a fresh, unpredictable, exclusive
    `0600` temp file in the same run directory (`open_private_exclusive`),
    flush and `fsync` it, close it, `os.replace()` it onto `run.json` on the
    same filesystem, then best-effort `fsync` the directory (a durability
    refinement some filesystems do not support, so a failure there is never
    fatal). A failure at any other step raises `LoggingError` and removes
    only its own temp file -- a previously persisted, valid `run.json` is
    never touched, recovered, or presented as a second source of truth
    (ADR-008; FR-044, FR-052; AC-033).

    Raising is the whole fail-closed contract here: this function does not
    -- and, with no orchestrator yet built, cannot -- itself stop further
    invocations; that a caller sees this exception and halts is exactly the
    mechanism the design relies on instead of silently treating a failed
    write as success.
    """

    if type(record) is not RunRecord:
        raise TypeError("record must be RunRecord")

    try:
        payload = serialize_run_record(record)
    except (TypeError, ValueError) as error:
        raise LoggingError(
            "runlog.run_record_serialize_failed",
            "failed to serialize the run record",
            technical_detail=type(error).__name__,
        ) from None

    run_directory = record.artifact_path.parent
    temp_path = run_directory / _temp_document_name()

    file_descriptor = open_private_exclusive(temp_path)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        _best_effort_unlink(temp_path)
        raise LoggingError(
            "runlog.run_record_write_failed",
            f"failed to write the run record: {temp_path}",
            technical_detail=type(error).__name__,
        ) from None

    try:
        os.replace(temp_path, record.artifact_path)
    except OSError as error:
        _best_effort_unlink(temp_path)
        raise LoggingError(
            "runlog.run_record_replace_failed",
            f"failed to atomically replace: {record.artifact_path}",
            technical_detail=type(error).__name__,
        ) from None

    _fsync_directory_best_effort(run_directory)


__all__ = (
    "DIRECTORY_MODE",
    "FILE_MODE",
    "RUN_ID_PATTERN",
    "allocate_run_directory",
    "attempt_log_filename",
    "create_run_directory",
    "format_run_id",
    "generate_run_id",
    "open_private_exclusive",
    "persist_run_record",
    "serialize_run_record",
)
