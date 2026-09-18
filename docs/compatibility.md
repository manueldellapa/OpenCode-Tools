# OpenCode compatibility

This document tracks which exact OpenCode version(s) OpenCode-Tools has
qualified, per ADR-005 (exact-version compatibility and agent identity proof)
and ADR-009 (POSIX platform baseline). It is an implementation-status record,
not a planning document: precedence for requirements still runs through the
PRD, System Design, and ADRs listed in `CLAUDE.md`.

## OpenCode `1.17.18`

**Status: supported (M15-03, issue #59), 2026-09-17.**

`1.17.18` is the only version OpenCode-Tools targets in v0.1, and the only
one the exact-version registry now enables. It became a *candidate* when
the offline fixture pack in
[`tests/fixtures/opencode/1.17.18/`](../tests/fixtures/opencode/1.17.18/)
was built (work package M07-01, issue #21), then moved to *supported* after
all three of the following passed:

1. the OpenCode adapter (`src/opencode_tools/opencode.py`, M07-02 through
   M07-06) is implemented and passes its full unit/component suite against
   this fixture pack, including exact-version selection, capability and
   control-plane preflight, the command builder and flag deny-list, the
   NDJSON transport decoder, sanitized-export agent identity verification,
   and the provider classifier;
2. the compatibility smoke test (M15-03, AC-027) ran a disposable,
   opt-in-only invocation against a real, locally installed `1.17.18`
   binary and the resulting artifacts were inspected, with no automatic
   publication or GitHub mutation;
3. the exact-version registry
   (`tests/fixtures/opencode/1.17.18/MANIFEST.json`'s `compatibility_status`)
   is updated to `"supported"`.

Step 1 was already done by the CLI command (M14, see
[`README.md`](../README.md)), which wires this adapter into
`opencode_tools.cli.main` end to end -- composition root, pipeline
execution, and terminal rendering, covered by `tests/unit/test_cli.py` and
`tests/component/test_cli_end_to_end.py` including a full
architect/coder/reviewer run against faked `opencode` and `gh` processes.
That alone never substituted for step 2, since every one of those tests
exercises a fixture or a fake `opencode` executable, never a real, running
OpenCode process. Step 2 is what actually happened on 2026-09-17: two
independent full runs of `tests/integration/test_opencode_1_17_18_smoke.py
-m live`, against a disposable private GitHub repository and a single
controlled issue on this project's own macOS (darwin) development machine,
both `PASSED` with an identical evidence shape:

- exact version `1.17.18` (`opencode --version`), no nearby-version
  fallback;
- the real, project-local `architect`/`coder`/`reviewer` agent definitions
  and their own configured provider/model (openrouter-hosted), resolved
  with zero fallback to a default agent, model, or provider -- confirmed
  both via `opencode debug agent <role>` preflight and via each attempt's
  sanitized-export `verified_agent` matching the requested role exactly;
- three distinct `session_id`s, one per role, never reused across attempts;
- `git_safety_status: SAFE` throughout, and the disposable target's remote
  `HEAD`, local `HEAD`, and commit count were all unchanged after the run
  -- no publication, no push, no GitHub mutation was observed;
- `final_status: APPROVED`, `terminal_outcome: SUCCEEDED` on both runs: the
  architect retrieved the real issue via the single `gh issue view`
  command its own least-privilege bash policy allows, the coder made the
  requested single-file change, and the reviewer approved it.

This evidence is macOS (darwin) only -- the live smoke was not re-run on
Linux in this qualification. That does not narrow v0.1's declared platform
support (ADR-009 already covers macOS and Linux, and the deterministic QG
and acceptance suite are independently qualified on both, see "Platform
baseline" below); it means the *live OpenCode process* half of AC-027
specifically has direct evidence from macOS only.

The preflight in ADR-005 still accepts no version other than the one
recorded here as supported: there is no fallback to "best effort," to a
nearby version, or to a default agent. If the identity proof or the smoke
test ever fails for `1.17.18` again, the version reverts to unsupported and
ADR-005 must be re-reviewed; a fallback is not an acceptable workaround.

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

**Provider classifier: a second trusted transport shape (issue #80),
2026-09-17.** The first production-like run against a real target (issue
#80's own evidence, run `20260917T124944.374451Z-bfee6257f6f8`) surfaced a
genuine `1.17.18` event `classify_provider_signal` did not yet recognize:
a top-level `error` event -- `session.error`'s untagged sibling type --
whose `error.data.message` is itself a serialized JSON string carrying a
transient Nvidia/OpenRouter overload (`code: 503`,
`metadata.error_type: "provider_overloaded"`). It fell through to
`PROCESS_ERROR` with `provider_diagnostic: null` instead of
`PROVIDER_ERROR`. `src/opencode_tools/opencode.py`'s classifier now
recognizes this second shape too, gated by its own narrow, structurally
exact allowlist (fail-closed on any malformed or unlisted variant, exactly
like the existing `session.error` path); the coder target-change retry
suppression (System Design SS11.3/SS12.2) is unaffected, since it never
depended on which transport shape produced the diagnostic. Two fixtures
were added -- `provider/nested-error-provider-overloaded.ndjson` (trusted)
and `provider/lookalike-nested-error-in-tool-output.ndjson` (trust
boundary) -- sanitized from and structurally faithful to that capture; no
existing fixture's bytes changed, and `compatibility_status` remains
`"supported"`.

**Transport: intermediate completed text no longer rejected as ambiguous
(issue #85), 2026-09-18.** The first production-like coder run against a
real target (House-Hold-Hub/Backend#1, run outcome `PROTOCOL_ERROR`, CLI
exit code 20) surfaced a genuine `1.17.18` session shape
`decode_run_transport` did not yet handle: the coder process itself exited
`0` and produced the requested filesystem changes, but the captured
NDJSON contained two distinct completed `text` `messageID`s -- an
intermediate one, followed by further `step_start`/`tool_use`/`step_finish`
activity, then a later, structurally terminal one carrying the canonical
`AGENT_STATUS: COMPLETED` marker. The adapter previously treated any more
than one completed `messageID` as unconditionally ambiguous and rejected
the stream before `protocol.py` ever saw the terminal text, so a valid
coder invocation could never reach the reviewer phase.
`src/opencode_tools/opencode.py`'s `decode_run_transport` now derives
terminality from verified `step_finish` lifecycle structure whenever the
stream carries at least one such event: the terminal candidate is the
completed group whose `messageID` matches the *last* `step_finish` event
observed before the stream ends, and every other completed group is
excluded as intermediate text -- this applies even when only one
`messageID` ever completes text, since a later `step_finish` for a
*different*, still-textless message means that later step's own
conclusion was never accounted for, so the earlier text cannot be proven
terminal either -- and that `step_finish` must genuinely be the stream's
last word. Any recognized message-lifecycle event (`step_start`,
`tool_use`, `reasoning`, `error`, or `text`, complete or not) observed
after the last `step_finish` disqualifies it too, even when a stream is
truncated right after that trailing activity with no further
`step_finish` at all: the earlier `step_finish`'s own claim to being the
end of the stream is unproven, so the text it would otherwise point to
cannot be trusted either. A later `step_finish` clears this and
re-establishes a new, provisionally trusted boundary -- the ordinary
multi-step shape `run/coder-intermediate-then-terminal-text.ndjson`
models keeps working exactly as before. Only when the stream carries no
`step_finish` event at all is a lone completed candidate trusted without
this check, matching the adapter's pre-#85 behavior for the many
hand-authored fixtures that never emit `step_finish` at all. A stream
that still cannot resolve to exactly one terminal group -- no
`step_finish` events present with more than one completed candidate, the
last `step_finish` resolves to a `messageID` with no completed text, or
unresolved activity trails the last `step_finish` -- continues to fail
closed with the same `opencode.transport_multiple_terminal_candidates`
code, exactly preserving the existing
`malformed/multi-terminal-candidates.ndjson` fixture's behavior. Since
`step_finish`'s own `messageID` is now load-bearing, a `step_finish`
event whose `messageID` is missing, `null`, empty, or not a string is
never silently treated as carrying no lifecycle information (unlike
`step_start`/`tool_use`/`error`, which stay fully inert regardless of
their `part` shape) -- it fails closed immediately with
`opencode.transport_invalid_event`, the same as a malformed `text`
part. `step_finish`'s `part.type` gets the same scrutiny a `text` part's
`part.type` already had: it must equal the real `1.17.18` `"step-finish"`
value exactly, so a missing, wrongly-typed, or lookalike value (e.g. the
sibling `"step-start"`) fails closed the same way instead of being
treated as an inert, unrecognized lifecycle event. One new fixture,
`run/coder-intermediate-then-terminal-text.ndjson`, sanitized from and
structurally faithful to the real capture, was added; no existing
fixture's bytes changed, and `compatibility_status` remains
`"supported"`.

### Platform baseline

Per ADR-009, only macOS and Linux on a local POSIX filesystem are supported,
with Python 3.13+, Git, the GitHub CLI, and a qualified OpenCode version all
resolvable from `PATH`. Windows native and Windows-mounted WSL filesystems
are not supported; an experimental WSL setup on a Linux filesystem does not
by itself satisfy the v0.1 acceptance criteria.

**Deterministic QG and acceptance suite: qualified separately on macOS and
Linux (M15-02, issue #58), 2026-09-16.** This qualifies the full quality
gate and deterministic acceptance suite on each platform's own local POSIX
filesystem -- it is a separate gate from OpenCode `1.17.18` itself (covered
above under "OpenCode `1.17.18`", M15-03), and it is not a smoke test
against a real OpenCode process; the live smoke's own direct evidence
remains macOS-only, as noted above.

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
filesystems; a Windows adapter. Each platform's result above was measured
directly and independently -- neither is inferred from the other's
outcome, and this qualification does not widen v0.1's declared support
scope beyond what ADR-009 already states. (OpenCode's live compatibility
smoke, AC-027/M15-03, is a separate gate, covered above under "OpenCode
`1.17.18`" -- its own direct evidence is macOS-only, not excluded.)

## See also

- [`README.md`](../README.md) -- the command this compatibility baseline
  applies to.
- [`docs/security-and-privacy.md`](security-and-privacy.md) -- what a real
  OpenCode/provider run would see and log, once qualified.
