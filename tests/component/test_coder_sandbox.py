"""Component coverage for the disposable coder Git sandbox (issue #90)."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_tools import (
    coder_sandbox,  # see test below for why the module itself is imported
)
from opencode_tools.coder_sandbox import (
    CoderSandbox,
    CoderSandboxError,
    _capture_raw_snapshot,  # see test below for why this private helper is imported
    cleanup_coder_sandbox,
    prepare_coder_sandbox,
    promote_coder_changes,
)
from opencode_tools.domain import ProcessResult, ProcessSpec, TargetRepository
from opencode_tools.ports import AttemptLogSink
from opencode_tools.process import SubprocessRunner


class _RecordingProcessRunner:
    """Wraps a real ProcessRunner and records every spec's argv and stdin."""

    def __init__(self, inner: SubprocessRunner) -> None:
        self._inner = inner
        self.recorded_argv: list[tuple[str, ...]] = []
        self.recorded_specs: list[ProcessSpec] = []

    def run(self, spec: ProcessSpec, *, sink: AttemptLogSink) -> ProcessResult:
        self.recorded_argv.append(spec.argv)
        self.recorded_specs.append(spec)
        return self._inner.run(spec, sink=sink)


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _git(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, TargetRepository]:
    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "README.md").write_text("before\n", encoding="utf-8")
    (root / "REMOVE.txt").write_text("remove me\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    return root, TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )


def _prepare(
    target: TargetRepository,
) -> tuple[Path, RealClock, SubprocessRunner, CoderSandbox]:
    git = shutil.which("git")
    assert git is not None
    clock = RealClock()
    runner = SubprocessRunner(clock)
    sandbox = prepare_coder_sandbox(
        runner,
        git_executable=Path(git),
        target=target,
        clock=clock,
        utility_timeout_seconds=10,
        termination_grace_seconds=1,
    )
    return Path(git), clock, runner, sandbox


def test_coder_sandbox_promotes_only_working_tree_delta(tmp_path: Path) -> None:
    root, target = _repository(tmp_path)
    original_head = _git(["rev-parse", "HEAD"], cwd=root)
    original_count = _git(["rev-list", "--count", "HEAD"], cwd=root)

    git, clock, runner, sandbox = _prepare(target)
    try:
        assert sandbox.root != root
        assert (sandbox.root / ".git").is_dir()
        assert _git(["remote"], cwd=sandbox.root) == ""
        assert not (sandbox.root / ".git/objects/info/alternates").exists()

        (sandbox.root / "README.md").write_text("after\n", encoding="utf-8")
        (sandbox.root / "REMOVE.txt").unlink()
        (sandbox.root / "NEW.txt").write_text("new\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "after\n"
    assert not (root / "REMOVE.txt").exists()
    assert (root / "NEW.txt").read_text(encoding="utf-8") == "new\n"
    assert _git(["rev-parse", "HEAD"], cwd=root) == original_head
    assert _git(["rev-list", "--count", "HEAD"], cwd=root) == original_count


def test_destroying_and_reinitializing_sandbox_git_cannot_damage_target(
    tmp_path: Path,
) -> None:
    root, target = _repository(tmp_path)
    original_head = _git(["rev-parse", "HEAD"], cwd=root)
    original_log = _git(["log", "--oneline", "--decorate"], cwd=root)

    git, clock, runner, sandbox = _prepare(target)
    try:
        shutil.rmtree(sandbox.root / ".git")
        _git(["init", "--quiet", "--initial-branch=main"], cwd=sandbox.root)
        (sandbox.root / "README.md").write_text("destroyed sandbox\n", encoding="utf-8")

        with pytest.raises(CoderSandboxError, match="sandbox Git metadata|HEAD"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / ".git").is_dir()
    assert _git(["rev-parse", "HEAD"], cwd=root) == original_head
    assert _git(["log", "--oneline", "--decorate"], cwd=root) == original_log
    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_target_drift_blocks_sandbox_promotion(tmp_path: Path) -> None:
    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (root / "README.md").write_text("external change\n", encoding="utf-8")

        with pytest.raises(CoderSandboxError, match="real target changed"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "external change\n"


def test_promotion_refuses_and_never_runs_a_coder_planted_clean_filter(
    tmp_path: Path,
) -> None:
    """GH #110: a coder-declared clean filter must never execute during promotion."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker"

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (sandbox.root / ".gitattributes").write_text(
            "* filter=evil\n", encoding="utf-8"
        )
        _git(
            [
                "config",
                "--local",
                "filter.evil.clean",
                f"touch {marker} && cat",
            ],
            cwd=sandbox.root,
        )

        with pytest.raises(CoderSandboxError, match="gitattributes"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_a_coder_planted_git_config_change_alone(
    tmp_path: Path,
) -> None:
    """GH #110: a config-only driver (e.g. diff.external) must also block promotion."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker-config-only"

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        _git(
            ["config", "--local", "diff.external", f"sh -c 'touch {marker}'"],
            cwd=sandbox.root,
        )

        with pytest.raises(CoderSandboxError, match="Git configuration"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_a_coder_planted_local_info_attributes(
    tmp_path: Path,
) -> None:
    """GH #110: an untracked .git/info/attributes override must also block promotion."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (sandbox.root / ".git" / "info" / "attributes").write_text(
            "* filter=evil\n", encoding="utf-8"
        )

        with pytest.raises(CoderSandboxError, match="attribute overrides"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_a_missing_git_config_as_a_sandbox_error(
    tmp_path: Path,
) -> None:
    """GH #110 review: a missing .git/config must raise CoderSandboxError, not FileNotFoundError."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (sandbox.root / ".git" / "config").unlink()

        with pytest.raises(CoderSandboxError, match="Git configuration"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_gitattributes_replaced_with_a_symlink(
    tmp_path: Path,
) -> None:
    """GH #110 review: an integrity file must never be read through a symlink."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        os.symlink(sandbox.root / "README.md", sandbox.root / ".gitattributes")

        with pytest.raises(CoderSandboxError, match="non-symlinked"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_refuses_git_info_attributes_replaced_with_a_fifo(
    tmp_path: Path,
) -> None:
    """GH #110 review: a non-regular file (e.g. a FIFO) must be rejected, never read."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        os.mkfifo(sandbox.root / ".git" / "info" / "attributes")

        with pytest.raises(CoderSandboxError, match="not a regular file"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_never_invokes_git_add(tmp_path: Path) -> None:
    """GH #110 P1 review: the promotion path must never stage by path at all."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    recorder = _RecordingProcessRunner(runner)
    try:
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (sandbox.root / "REMOVE.txt").unlink()
        (sandbox.root / "NEW.txt").write_text("new\n", encoding="utf-8")

        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert recorder.recorded_argv, "expected at least one git invocation"
    for argv in recorder.recorded_argv:
        assert "add" not in argv, f"promotion invoked a staging command: {argv}"
    assert (root / "README.md").read_text(encoding="utf-8") == "coder change\n"
    assert not (root / "REMOVE.txt").exists()
    assert (root / "NEW.txt").read_text(encoding="utf-8") == "new\n"


def test_promotion_ignores_a_nested_gitattributes_clean_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH #110 P1: a clean filter reachable only via a NESTED .gitattributes,
    with the filter driver itself only defined in global/system config (so
    neither the root .gitattributes nor the sandbox's local .git/config
    baseline ever changes), must still never execute."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker-nested"
    global_gitconfig = tmp_path / "fake-global-gitconfig"
    global_gitconfig.write_text(
        f'[filter "evil"]\n\tclean = touch {marker} && cat\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_gitconfig))

    git, clock, runner, sandbox = _prepare(target)
    try:
        nested = sandbox.root / "subdir"
        nested.mkdir()
        (nested / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (nested / "payload.txt").write_text("nested coder change\n", encoding="utf-8")

        # Neither integrity baseline changed -- the root .gitattributes and
        # the sandbox's local .git/config are untouched by this attack.
        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "subdir" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "nested coder change\n"
    assert (root / "subdir" / ".gitattributes").read_text(
        encoding="utf-8"
    ) == "* filter=evil\n"


def test_promotion_ignores_diff_external_and_textconv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH #110 P1: neither a global diff.external nor a nested-attribute
    textconv driver may run while building the promotion patch."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker-diff"
    global_gitconfig = tmp_path / "fake-global-gitconfig-diff"
    global_gitconfig.write_text(
        f"[diff]\n\texternal = sh -c 'touch {marker}'\n"
        f"[diff \"evil\"]\n\ttextconv = sh -c 'touch {marker}; cat'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_gitconfig))

    git, clock, runner, sandbox = _prepare(target)
    try:
        nested = sandbox.root / "subdir"
        nested.mkdir()
        (nested / ".gitattributes").write_text("* diff=evil\n", encoding="utf-8")
        (sandbox.root / "README.md").write_text("coder change\n", encoding="utf-8")
        (nested / "payload.txt").write_text("nested payload\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "coder change\n"
    assert (root / "subdir" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "nested payload\n"


def test_promotion_handles_add_modify_delete_binary_and_executable_mode(
    tmp_path: Path,
) -> None:
    """GH #110 P1: the plumbing-based patch must still cover the full shape
    of a legitimate change set, not only plain text modifications."""

    root, target = _repository(tmp_path)
    binary_payload = bytes(range(256)) * 4

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "README.md").write_text("modified\n", encoding="utf-8")
        (sandbox.root / "REMOVE.txt").unlink()
        (sandbox.root / "asset.bin").write_bytes(binary_payload)
        script = sandbox.root / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        script.chmod(0o755)

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "modified\n"
    assert not (root / "REMOVE.txt").exists()
    assert (root / "asset.bin").read_bytes() == binary_payload
    promoted_script = root / "run.sh"
    assert promoted_script.read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"
    assert promoted_script.stat().st_mode & 0o111, "executable bit was not promoted"


def test_promotion_excludes_gitignored_untracked_files(tmp_path: Path) -> None:
    """GH #110 P1 review: ignored untracked files/directories must never be
    promoted, matching what `git add -A` itself would have excluded."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / ".gitignore").write_text(
            "ignored.txt\nbuild/\n", encoding="utf-8"
        )
        (sandbox.root / "ignored.txt").write_text(
            "should not be promoted\n", encoding="utf-8"
        )
        build_dir = sandbox.root / "build"
        build_dir.mkdir()
        (build_dir / "artifact.bin").write_bytes(b"\x00\x01\x02\x03")
        (sandbox.root / "NEW.txt").write_text("legitimate change\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / ".gitignore").read_text(encoding="utf-8") == "ignored.txt\nbuild/\n"
    assert (root / "NEW.txt").read_text(encoding="utf-8") == "legitimate change\n"
    assert not (root / "ignored.txt").exists()
    assert not (root / "build").exists()


def test_promotion_handles_filenames_with_newline_tab_and_space(
    tmp_path: Path,
) -> None:
    """GH #110 P2 review: a filename containing a literal newline, tab, or
    space must not corrupt promotion or fail an unrelated change."""

    root, target = _repository(tmp_path)
    tricky_names = [
        "file\nwith\nnewline.txt",
        "file\twith\ttab.txt",
        "file with space.txt",
    ]

    git, clock, runner, sandbox = _prepare(target)
    try:
        for name in tricky_names:
            (sandbox.root / name).write_text(f"content: {name!r}\n", encoding="utf-8")
        (sandbox.root / "README.md").write_text("also modified\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "also modified\n"
    for name in tricky_names:
        assert (root / name).read_text(encoding="utf-8") == f"content: {name!r}\n"


def test_promotion_preserves_checkout_normalized_files_left_untouched(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: a file only affected by checkout-time EOL
    normalization (e.g. `eol=crlf`) must keep its exact baseline blob when
    the coder never actually touches it -- not be "promoted" back to its
    raw, CRLF-converted checkout form."""

    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / ".gitattributes").write_text("*.txt text eol=crlf\n", encoding="utf-8")
    (root / "file.txt").write_bytes(b"line1\nline2\n")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    target = TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )
    original_bytes = (root / "file.txt").read_bytes()

    git, clock, runner, sandbox = _prepare(target)
    try:
        # Confirm the sandbox checkout actually materialized CRLF, so this
        # test exercises real checkout normalization, not a no-op.
        assert b"\r\n" in (sandbox.root / "file.txt").read_bytes()

        (sandbox.root / "NEW.txt").write_text("new\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "file.txt").read_bytes() == original_bytes
    assert (root / "NEW.txt").read_text(encoding="utf-8") == "new\n"


def test_promotion_refuses_an_unreadable_new_candidate_file(tmp_path: Path) -> None:
    """GH #110 P2/P1 review: a coder-unreadable brand-new file (e.g.
    `chmod 000`) must raise CoderSandboxError, not a raw PermissionError.

    A brand-new path is classified without an in-process content read
    (GH #110 follow-up), so `chmod 000` alone does not fail at
    classification -- `lstat` does not require read permission -- and the
    failure is instead surfaced by the bounded `git hash-object`
    subprocess itself refusing to open the file, which `_run_git` already
    converts to a `CoderSandboxError` rather than letting a raw process
    failure escape."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    secret = sandbox.root / "secret.txt"
    try:
        secret.write_text("top secret\n", encoding="utf-8")
        secret.chmod(0o000)

        with pytest.raises(CoderSandboxError):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        secret.chmod(0o644)
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"
    assert not (root / "secret.txt").exists()


def test_promotion_refuses_an_unreadable_changed_tracked_file(tmp_path: Path) -> None:
    """GH #110 P2 review: a coder-unreadable *tracked* file that was
    changed (e.g. `chmod 000` after editing) must still raise
    CoderSandboxError from the in-process classification/fingerprint step
    -- the fast path for brand-new candidates above does not apply here,
    since this path is present in `pristine_raw_snapshot` and must still
    be fingerprinted to prove it actually changed."""

    root, target = _repository(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    tracked = sandbox.root / "README.md"
    try:
        tracked.write_text("changed\n", encoding="utf-8")
        tracked.chmod(0o000)

        with pytest.raises(CoderSandboxError, match="could not be read"):
            promote_coder_changes(
                runner,
                git_executable=git,
                target=target,
                sandbox=sandbox,
                clock=clock,
                utility_timeout_seconds=10,
                termination_grace_seconds=1,
            )
    finally:
        tracked.chmod(0o644)
        cleanup_coder_sandbox(sandbox)

    assert (root / "README.md").read_text(encoding="utf-8") == "before\n"


def test_promotion_only_hashes_genuinely_changed_files(tmp_path: Path) -> None:
    """GH #110 P1 review: unchanged files must never spawn `git hash-object`
    -- only a path actually proven to differ from the pristine snapshot
    may, so subprocess count scales with the size of the coder's real
    change, not with the size of the repository."""

    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    file_count = 50
    for i in range(file_count):
        (root / f"file{i:03d}.txt").write_text(f"content {i}\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    target = TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )

    git, clock, runner, sandbox = _prepare(target)
    recorder = _RecordingProcessRunner(runner)
    try:
        # Nothing changed at all: promotion must not hash anything.
        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
        noop_hash_calls = [
            argv for argv in recorder.recorded_argv if "hash-object" in argv
        ]
        assert noop_hash_calls == [], (
            f"expected no hash-object calls for a no-op promotion, got {noop_hash_calls}"
        )

        # Exactly one real change among `file_count` tracked files.
        (sandbox.root / "file000.txt").write_text("changed\n", encoding="utf-8")
        recorder.recorded_argv.clear()
        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    hash_calls = [argv for argv in recorder.recorded_argv if "hash-object" in argv]
    assert len(hash_calls) == 1, (
        f"expected exactly 1 hash-object call, got {hash_calls}"
    )
    assert (root / "file000.txt").read_text(encoding="utf-8") == "changed\n"


def test_promotion_handles_directory_replaced_by_file(tmp_path: Path) -> None:
    """GH #110 P2 review: a coder replacing a tracked directory with a
    regular file must promote as a clean deletion + addition, not abort
    with a raw NotADirectoryError."""

    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "dir").mkdir()
    (root / "dir" / "child.txt").write_text("child\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    target = TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )

    git, clock, runner, sandbox = _prepare(target)
    try:
        shutil.rmtree(sandbox.root / "dir")
        (sandbox.root / "dir").write_text("now a file\n", encoding="utf-8")

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "dir").is_file()
    assert (root / "dir").read_text(encoding="utf-8") == "now a file\n"


def test_promotion_handles_file_replaced_by_directory(tmp_path: Path) -> None:
    """GH #110 P2 review: a coder replacing a tracked file with a
    (nonempty) directory must promote via force-remove -- the old entry
    still existing on disk, just as a directory, must not block removing
    it from the index."""

    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "node").write_text("old file content\n", encoding="utf-8")
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    target = TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "node").unlink()
        (sandbox.root / "node").mkdir()
        (sandbox.root / "node" / "child.js").write_text(
            "child content\n", encoding="utf-8"
        )

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "node").is_dir()
    assert (root / "node" / "child.js").read_text(encoding="utf-8") == "child content\n"


def test_capture_raw_snapshot_fingerprints_a_large_file_without_buffering_it_whole(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: fingerprinting a large tracked file must never
    hold its full content in memory -- peak Python allocation must stay
    close to the chunk size, not the file size.

    This calls `_capture_raw_snapshot` directly, on a plain repository,
    rather than going through `prepare_coder_sandbox` on a real target:
    `prepare_coder_sandbox` also runs `git_safety.capture_git_state` on
    the target, which computes its own, separate content-sensitive
    fingerprint of every file (by design, unrelated to this fix) and
    would otherwise dominate the measurement, hiding a regression in the
    code this fix actually touches.
    """

    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    large_size = 16 * 1024 * 1024  # 16 MiB: well beyond the 1 MiB read chunk
    large_payload = (bytes(range(256)) * ((large_size // 256) + 1))[:large_size]
    (root / "large.bin").write_bytes(large_payload)
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)

    git = shutil.which("git")
    assert git is not None
    clock = RealClock()
    runner = SubprocessRunner(clock)

    tracemalloc.start()
    try:
        snapshot = _capture_raw_snapshot(
            runner,
            git_executable=Path(git),
            sandbox_root=root,
            utility_timeout_seconds=30,
            termination_grace_seconds=1,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "large.bin" in snapshot
    assert peak < large_size // 4, (
        f"_capture_raw_snapshot allocated {peak} Python-level bytes while "
        f"fingerprinting a {large_size}-byte tracked file -- expected it to "
        "stay close to the chunk size, not the file size"
    )


def test_promotion_of_a_large_changed_file_still_blocks_clean_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH #110 P1 review: switching a genuinely changed regular file's
    hashing to a pathname-based `git hash-object` call must still never
    execute a coder-planted clean filter, even for a large file."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "pwned-marker-large"
    global_gitconfig = tmp_path / "fake-global-gitconfig-large"
    global_gitconfig.write_text(
        f'[filter "evil"]\n\tclean = touch {marker} && cat\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_gitconfig))
    large_size = 8 * 1024 * 1024  # large enough to exercise the pathname-based hash
    large_payload = (bytes(range(256)) * ((large_size // 256) + 1))[:large_size]

    git, clock, runner, sandbox = _prepare(target)
    try:
        nested = sandbox.root / "subdir"
        nested.mkdir()
        (nested / ".gitattributes").write_text(
            "large.bin filter=evil\n", encoding="utf-8"
        )
        (nested / "large.bin").write_bytes(large_payload)

        # Neither integrity baseline changed -- the root .gitattributes and
        # the sandbox's local .git/config are untouched by this attack, and
        # the filter driver lives only in (fake) global config, so this
        # exercises the pathname-based hash-object call for a genuinely
        # new, large candidate rather than being blocked by the earlier
        # defense-in-depth checks.
        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=30,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists()
    assert (root / "subdir" / "large.bin").read_bytes() == large_payload


def _repository_with_submodule(tmp_path: Path) -> tuple[Path, TargetRepository, str]:
    """A target repo with a submodule gitlink, plus the gitlink's sha.

    `prepare_coder_sandbox` clones with `--no-recurse-submodules`, so the
    sandbox never initializes it -- `submod` is an empty directory on
    disk while the index still carries the `160000` entry.
    """
    sub_root = tmp_path / "sub"
    sub_root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=sub_root)
    (sub_root / "f.txt").write_text("hi\n", encoding="utf-8")
    _git(["add", "-A"], cwd=sub_root)
    _git(["commit", "--quiet", "-m", "sub initial"], cwd=sub_root)

    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "README.md").write_text("before\n", encoding="utf-8")
    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "--quiet",
            "add",
            str(sub_root),
            "submod",
        ],
        cwd=root,
    )
    # `submodule add` only stages `.gitmodules` and the gitlink -- stage
    # README.md too so it is a genuinely tracked file at baseline.
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    gitlink_sha = _git(["rev-parse", "HEAD:submod"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    return (
        root,
        TargetRepository(
            root=root.resolve(),
            workspace_relative=Path("."),
            git_common_dir=git_dir.resolve(),
        ),
        gitlink_sha,
    )


def _assert_submod_never_proposed_for_removal(
    recorder: _RecordingProcessRunner,
) -> None:
    """The precise signal for this bug: whether `submod` was ever fed to
    `update-index --force-remove`, independent of whether `git apply`
    happens to make that particular hunk a visible no-op afterwards on a
    given target layout."""
    for spec in recorder.recorded_specs:
        if "--force-remove" in spec.argv:
            stdin = spec.stdin or b""
            assert b"submod" not in stdin, (
                f"submod was proposed for index removal: {stdin!r}"
            )


def test_noop_promotion_preserves_an_uninitialized_submodule_gitlink(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: a no-op promotion must not delete a tracked
    submodule reference just because it is checked out as an empty
    directory (or missing entirely) in the coder-writable sandbox."""

    root, target, gitlink_sha = _repository_with_submodule(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    recorder = _RecordingProcessRunner(runner)
    try:
        # Genuinely nothing changed: no coder edits at all.
        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    apply_calls = [argv for argv in recorder.recorded_argv if "apply" in argv]
    assert apply_calls == [], (
        f"expected no git apply call for a true no-op, got {apply_calls}"
    )
    _assert_submod_never_proposed_for_removal(recorder)
    assert _git(["rev-parse", "HEAD:submod"], cwd=root) == gitlink_sha


def test_unrelated_promotion_preserves_an_uninitialized_submodule_gitlink(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: promoting an unrelated coder change must not
    propose deleting a tracked submodule reference."""

    root, target, gitlink_sha = _repository_with_submodule(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    recorder = _RecordingProcessRunner(runner)
    try:
        (sandbox.root / "README.md").write_text("after\n", encoding="utf-8")
        (sandbox.root / "NEW.txt").write_text("new\n", encoding="utf-8")

        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    _assert_submod_never_proposed_for_removal(recorder)

    assert (root / "README.md").read_text(encoding="utf-8") == "after\n"
    assert (root / "NEW.txt").read_text(encoding="utf-8") == "new\n"
    assert _git(["rev-parse", "HEAD:submod"], cwd=root) == gitlink_sha
    assert (root / "submod").is_dir()


def _repository_with_tracked_subdirectory(
    tmp_path: Path,
) -> tuple[Path, TargetRepository]:
    """A target repo where `dir/child.txt` is tracked at baseline, so the
    sandbox's own HEAD (and `prepare_coder_sandbox`'s pristine snapshot)
    genuinely includes it before any coder edit."""
    root = tmp_path / "target"
    root.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], cwd=root)
    (root / "dir").mkdir()
    (root / "dir" / "child.txt").write_text(
        "original tracked content\n", encoding="utf-8"
    )
    _git(["add", "-A"], cwd=root)
    _git(["commit", "--quiet", "-m", "initial"], cwd=root)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], cwd=root))
    return root, TargetRepository(
        root=root.resolve(),
        workspace_relative=Path("."),
        git_common_dir=git_dir.resolve(),
    )


def test_promotion_handles_directory_replaced_by_symlink_with_cached_descendants(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: replacing a tracked directory with a symlink
    (to another directory inside the sandbox that happens to have a
    same-named child with *different* content) must never read through
    that symlinked ancestor to fingerprint/hash the old cached
    descendant -- it must be treated as deleted, and the symlink promoted
    as a symlink."""

    root, target = _repository_with_tracked_subdirectory(tmp_path)

    git, clock, runner, sandbox = _prepare(target)
    recorder = _RecordingProcessRunner(runner)
    try:
        other = sandbox.root / "other"
        other.mkdir()
        (other / "child.txt").write_text("different content\n", encoding="utf-8")

        shutil.rmtree(sandbox.root / "dir")
        os.symlink(other, sandbox.root / "dir")

        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "dir").is_symlink()
    assert os.readlink(root / "dir") == str(other)
    # The old cached dir/child.txt content must never have been read or
    # hashed through the new symlink. "different content" legitimately
    # appears in the final `git apply` patch (other/child.txt is its own,
    # unrelated, correctly-promoted new file) -- exclude only that call and
    # confirm no earlier classify/hash/index-build step ever saw it.
    for spec in recorder.recorded_specs:
        if "apply" in spec.argv:
            continue
        stdin = spec.stdin or b""
        assert b"different content" not in stdin, (
            f"content behind the new symlink was read: {stdin!r}"
        )
    # The old cached dir/child.txt path itself must have been removed from
    # the index, and never re-added (proving it was classified deleted,
    # not resolved through the new symlink).
    removed_dir_child = False
    for spec in recorder.recorded_specs:
        stdin = spec.stdin or b""
        if "--force-remove" in spec.argv and b"dir/child.txt" in stdin:
            removed_dir_child = True
        if "--index-info" in spec.argv:
            assert b"\tdir/child.txt\x00" not in stdin, (
                f"dir/child.txt was re-added to the index: {stdin!r}"
            )
    assert removed_dir_child, "dir/child.txt was never removed from the index"


def test_promotion_never_reads_external_content_through_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: a symlink pointing entirely outside the sandbox
    must never cause external filesystem content to be read into this
    process, let alone promoted, when resolving an old cached path
    beneath it."""

    root, target = _repository_with_tracked_subdirectory(tmp_path)
    secret_marker = b"SECRET EXTERNAL CONTENT SHOULD NEVER BE PROMOTED"
    external = tmp_path / "external-secret-location"
    external.mkdir()
    (external / "child.txt").write_bytes(secret_marker + b"\n")

    git, clock, runner, sandbox = _prepare(target)
    recorder = _RecordingProcessRunner(runner)
    try:
        shutil.rmtree(sandbox.root / "dir")
        os.symlink(external, sandbox.root / "dir")

        promote_coder_changes(
            recorder,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert (root / "dir").is_symlink()
    assert os.readlink(root / "dir") == str(external)
    # The decisive check: the external file's content must never have
    # reached this process at all, through any subprocess stdin.
    for spec in recorder.recorded_specs:
        stdin = spec.stdin or b""
        assert secret_marker not in stdin, (
            f"external content leaked into a subprocess call: {stdin!r}"
        )
    # The target's own real index (git apply without --index never
    # touches it) must still have the *original* dir/child.txt blob --
    # proof no external content was ever staged as its replacement.
    original_entries = _git(["ls-files", "--stage", "--", "dir/child.txt"], cwd=root)
    assert "dir/child.txt" in original_entries


def test_promotion_skips_fingerprinting_a_brand_new_file_and_hashes_it_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH #110 P1 review: a path absent from `pristine_raw_snapshot` is
    already known to be new -- there is nothing to compare it against, so
    fingerprinting its content first is redundant work with no timeout of
    its own (unlike the bounded `git hash-object` subprocess that follows
    either way). Promotion must classify a brand-new path's mode without
    ever reading its content in-process."""

    root, target = _repository(tmp_path)
    git, clock, runner, sandbox = _prepare(target)

    fingerprinted_paths: list[str] = []
    original_fingerprint = coder_sandbox._fingerprint_file_chunked

    def _spy(entry_path: Path, mode: str) -> str:
        fingerprinted_paths.append(str(entry_path))
        return original_fingerprint(entry_path, mode)

    monkeypatch.setattr(coder_sandbox, "_fingerprint_file_chunked", _spy)

    try:
        (sandbox.root / "brand-new.txt").write_text("new content\n", encoding="utf-8")
        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not any(p.endswith("brand-new.txt") for p in fingerprinted_paths), (
        f"a brand-new path was fingerprinted before being hashed: {fingerprinted_paths!r}"
    )
    assert (root / "brand-new.txt").read_text(encoding="utf-8") == "new content\n"


def test_promotion_never_executes_a_planted_fsmonitor_command(tmp_path: Path) -> None:
    """GH #110 P1 review: `.git/config` is checked against its pristine
    byte-identity baseline before promotion runs -- but a background
    process the coder leaves running could still rewrite it after that
    check completes and before the Git commands below actually run (a
    TOCTOU window the one-time check alone cannot close). This constructs
    exactly that end state -- a `.git/config` that already matches the
    (now-poisoned) baseline -- and confirms every trusted promotion Git
    invocation still refuses to execute an active `core.fsmonitor`
    command, because it is forced off regardless of what the sandbox's
    local config says."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "fsmonitor-marker"

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "new.txt").write_text("new\n", encoding="utf-8")
        _git(
            ["config", "core.fsmonitor", f"touch {marker}; true"],
            cwd=sandbox.root,
        )
        poisoned_config = (sandbox.root / ".git" / "config").read_bytes()
        poisoned_sandbox = dataclasses.replace(
            sandbox, git_config_baseline=poisoned_config
        )

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=poisoned_sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists(), "a planted core.fsmonitor command executed"
    assert (root / "new.txt").read_text(encoding="utf-8") == "new\n"


def test_promotion_never_executes_a_planted_post_index_change_hook(
    tmp_path: Path,
) -> None:
    """GH #110 P1 review: same TOCTOU rationale as the fsmonitor case
    above, for `core.hooksPath` -- an index-changing command
    (`update-index`) must never execute a coder-planted
    `post-index-change` hook, even when `.git/config` already matches the
    byte-identity baseline, because `core.hooksPath` is forced to
    `os.devnull` regardless of what the sandbox's local config says."""

    root, target = _repository(tmp_path)
    marker = tmp_path / "hook-marker"
    hooks_dir = tmp_path / "planted-hooks"
    hooks_dir.mkdir()
    hook_path = hooks_dir / "post-index-change"
    hook_path.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook_path.chmod(0o755)

    git, clock, runner, sandbox = _prepare(target)
    try:
        (sandbox.root / "new.txt").write_text("new\n", encoding="utf-8")
        _git(["config", "core.hooksPath", str(hooks_dir)], cwd=sandbox.root)
        poisoned_config = (sandbox.root / ".git" / "config").read_bytes()
        poisoned_sandbox = dataclasses.replace(
            sandbox, git_config_baseline=poisoned_config
        )

        promote_coder_changes(
            runner,
            git_executable=git,
            target=target,
            sandbox=poisoned_sandbox,
            clock=clock,
            utility_timeout_seconds=10,
            termination_grace_seconds=1,
        )
    finally:
        cleanup_coder_sandbox(sandbox)

    assert not marker.exists(), "a planted core.hooksPath hook executed"
    assert (root / "new.txt").read_text(encoding="utf-8") == "new\n"
