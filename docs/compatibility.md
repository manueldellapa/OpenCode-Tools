# OpenCode compatibility

This document tracks which exact OpenCode version(s) OpenCode-Tools has
qualified, per ADR-005 (exact-version compatibility and agent identity proof)
and ADR-009 (POSIX platform baseline). It is an implementation-status record,
not a planning document: precedence for requirements still runs through the
PRD, System Design, and ADRs listed in `CLAUDE.md`.

## OpenCode `1.17.18`

**Status: candidate, not supported.**

`1.17.18` is the only version OpenCode-Tools targets in v0.1. It became a
candidate when the offline fixture pack in
[`tests/fixtures/opencode/1.17.18/`](../tests/fixtures/opencode/1.17.18/)
was built (work package M07-01, issue #21). A candidate version has offline
evidence for the transport, capability, and identity contract described in
System Design SS10, but has not yet been proven against a real, running
OpenCode process.

A version only moves from *candidate* to *supported* after:

1. the OpenCode adapter (`src/opencode_tools/opencode.py`, M07-02 through
   M07-06) is implemented and passes its full unit/component suite against
   this fixture pack, including exact-version selection, capability and
   control-plane preflight, the command builder and flag deny-list, the
   NDJSON transport decoder, sanitized-export agent identity verification,
   and the provider classifier;
2. the compatibility smoke test (M15-03, AC-027) runs a disposable,
   opt-in-only invocation against a real, locally installed `1.17.18` binary
   and inspects the resulting artifacts, with no automatic publication or
   GitHub mutation;
3. the exact-version registry is updated to enable `1.17.18` as a supported
   runtime version.

Step 1 is done: the CLI command (M14, see [`README.md`](../README.md))
wires this adapter into `opencode_tools.cli.main` end to end -- composition
root, pipeline
execution, and terminal rendering are all implemented and covered by
`tests/unit/test_cli.py` and `tests/component/test_cli_end_to_end.py`,
including a full architect/coder/reviewer run against faked `opencode` and
`gh` processes. None of that substitutes for step 2: every test that
exercises the command does so against fixtures or a fake `opencode`
executable, never a real, running OpenCode process, so it proves the
command's own wiring and rendering, not OpenCode `1.17.18` compatibility.
Steps 2 and 3 above are both M15-03's gate and are still open; neither is
blocked by, or required for, any milestone before M15. (M15-02, covered
below under "Platform baseline," qualifies the deterministic QG and
acceptance suite on macOS and Linux -- a separate gate from OpenCode
`1.17.18` candidacy, and on its own it does not move `1.17.18` any closer
to *supported*.)

Until all three steps pass, the preflight in ADR-005 accepts no versions at
all: there is no fallback to "best effort," to a nearby version, or to a
default agent. If the identity proof or the smoke test ever fails for
`1.17.18`, the version reverts to unsupported and ADR-005 must be
re-reviewed; a fallback is not an acceptable workaround.

### Fixture pack

[`tests/fixtures/opencode/1.17.18/MANIFEST.json`](../tests/fixtures/opencode/1.17.18/MANIFEST.json)
is the provenance and integrity index for every fixture in the pack: each
entry records the fixture's category, positive/negative/trust-boundary
`kind`, requested agent `role` (when applicable), a SHA-256 digest of its
exact bytes, and a description. `tests/unit/test_opencode_adapter.py` proves
the pack's inventory, digests, and provenance metadata offline and
deterministically, without ever invoking OpenCode, a provider, or the
network. No fixture contains secrets, real model/provider identifiers, or
data captured from a live run.

Adding a new OpenCode version never edits this pack: it gets its own
`tests/fixtures/opencode/<version>/` directory and its own manifest, so the
`1.17.18` evidence and expectations stay immutable.

### Platform baseline

Per ADR-009, only macOS and Linux on a local POSIX filesystem are supported,
with Python 3.13+, Git, the GitHub CLI, and a qualified OpenCode version all
resolvable from `PATH`. Windows native and Windows-mounted WSL filesystems
are not supported; an experimental WSL setup on a Linux filesystem does not
by itself satisfy the v0.1 acceptance criteria.

**Deterministic QG and acceptance suite: qualified separately on macOS and
Linux (M15-02, issue #58), 2026-09-16.** This qualifies the full quality
gate and deterministic acceptance suite on each platform's own local POSIX
filesystem -- it does not qualify OpenCode `1.17.18` itself (that stays
"candidate, not supported" until M15-03 above passes), and it is not a
smoke test against a real OpenCode process.

| | macOS | Linux |
|---|---|---|
| Kernel | Darwin 25.6.0 (macOS 26.7, build 25G229), arm64 | Linux 7.0.12-linuxkit, aarch64 (Docker Desktop's own LinuxKit VM kernel -- a real, unmodified Linux kernel, not emulated on this Apple Silicon host) |
| Distribution | -- | Debian GNU/Linux 13 (trixie) |
| Filesystem | Native local APFS | The container's own overlay filesystem, local to the Linux VM -- the repository was copied into the image (`COPY`), never bind-mounted from the macOS host, so no host-filesystem semantics leak into the result |
| User | Interactive user, non-root | `uid=1000(runner)`, non-root (confirmed via `id`) -- chosen deliberately so permission-based fault-injection tests (chmod-based) exercise genuine Unix permission enforcement, which `root` bypasses |
| Python / Git | 3.13.15 / 2.54.0 (Apple Git-157) | 3.13.15 / 2.47.3 |
| `pytest` / `ruff` / `mypy` | 9.1.1 / 0.16.7 / 2.3.1 | 9.1.1 / 0.16.7 / 2.3.1 (independently resolved, not pinned -- happened to match exactly) |

Full `QG` result, each command run independently on each platform:

| Command | macOS | Linux |
|---|---|---|
| `python3.13 -m pytest` | 1230 passed, repeated 3x, all green | 1230 passed, repeated 3x, all green |
| `ruff check .` | All checks passed | All checks passed |
| `ruff format --check .` | 82 files already formatted | 83 files already formatted (a `ruff format` summary-count quirk only -- `ruff check --show-files` lists a byte-identical 61-file set on both platforms; the actual check outcome is 0 formatting violations on each) |
| `mypy --strict src tests` | Success, 60 source files | Success, 60 source files |

Focus suite named by the issue (process-group/termination, `flock`,
quarantine, runtime mode/atomic replace, Git path/fingerprint --
`tests/component/test_process_runner.py`, `test_locking.py`,
`test_runtime_store.py`, `test_git_repository.py`,
`tests/unit/test_git_fingerprint.py`): **143 passed, repeated 3x on each
platform, all green.**

One pre-existing test,
`tests/component/test_git_repository.py::test_git_state_v1_exceeded_deadline_on_a_large_repository_is_indeterminate`,
was flaky under this qualification (2 of 3 initial full-suite runs failed
on Linux, 0 of 3 on macOS) and was fixed as a test-only change before this
qualification was recorded as PASS. Root cause: the test drove a real
`git` subprocess against a `utility_timeout_seconds=0.001` (1ms) deadline
-- three orders of magnitude below `ExecutionConfig`'s validated floor of 1
second, a value production can never construct -- which raced
`SubprocessRunner`'s 10ms polling granularity against how fast each host's
`git` forks and exits, rather than deterministically exercising the
"deadline exceeded" path. The fix reuses this same file's existing
`_FakeDeadlineClock` (already used by a sibling deadline test) to drive
`SubprocessRunner`, making the timeout deterministic regardless of real
subprocess timing; no `src/opencode_tools/` change. After the fix: 15/15
repeats on each platform, and 3/3 full-suite and focus-suite repeats on
each platform, all green -- see the commit for the full diff and
reasoning.

Excluded scope, unchanged and not declared supported: Windows native;
WSL on a Windows-mounted filesystem; remote or otherwise non-POSIX
filesystems; a Windows adapter; OpenCode's live compatibility smoke
(AC-027, M15-03). Each platform's result above was measured directly and
independently -- neither is inferred from the other's outcome, and this
qualification does not widen v0.1's declared support scope beyond what
ADR-009 already states.

## See also

- [`README.md`](../README.md) -- the command this compatibility baseline
  applies to.
- [`docs/security-and-privacy.md`](security-and-privacy.md) -- what a real
  OpenCode/provider run would see and log, once qualified.
