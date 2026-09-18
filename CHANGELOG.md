# Changelog

All notable changes to this project are documented in this file. The format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Incomplete OpenCode tool-call lifecycle accepted as terminal (issue #87):
  `decode_run_transport` now rejects a final OpenCode 1.17.18
  `step_finish(reason="tool-calls")` at EOF with the dedicated
  `opencode.transport_incomplete_tool_call_lifecycle` diagnostic instead
  of trusting an associated completed text and falling through later to
  `protocol.marker_missing`. Completed text that is empty after
  `.strip()` is no longer a terminal candidate; a later blank completed
  write for the same `messageID` also invalidates any earlier candidate,
  preserving last-write-wins semantics. Intermediate `tool-calls` steps
  followed by a genuine terminal response remain valid.

- Intermediate completed text rejected as ambiguous (issue #85): a real
  OpenCode `1.17.18` coder invocation can complete more than one
  message's text in a single session -- an intermediate completed text
  followed by further tool/lifecycle activity, then a later, structurally
  terminal completed text carrying the canonical `AGENT_STATUS: COMPLETED`
  marker. `decode_run_transport` previously treated any more than one
  completed `messageID` as unconditionally ambiguous
  (`opencode.transport_multiple_terminal_candidates`), so a valid coder
  run could never reach the reviewer phase even though OpenCode exited
  successfully and produced the requested changes. It now derives
  terminality from verified `step_finish` lifecycle structure whenever the
  stream carries at least one such event: the completed group whose
  `messageID` matches the last `step_finish` event before the stream ends
  is terminal, and every other completed group is excluded as intermediate
  text -- this now applies even to a lone completed candidate, so one
  followed by unresolved `step_finish` activity for a different message is
  no longer trusted just because no second candidate exists. Streams that
  still cannot be resolved this way continue to fail closed with the same
  error code. A `step_finish` event whose own `messageID` is missing,
  `null`, empty, or not a string also fails closed immediately
  (`opencode.transport_invalid_event`) rather than being silently skipped
  as if it carried no lifecycle information, since that field now decides
  terminality. The chosen `step_finish` must also genuinely be the
  stream's last word: any recognized message-lifecycle event observed
  after it -- including trailing activity that never gets its own
  `step_finish` at all, e.g. a truncated capture -- disqualifies it too,
  closing a gap where a single, otherwise-clean completed candidate could
  still be trusted even though later, unconcluded activity followed it.
  `step_finish`'s `part.type` is now validated too, the same way a `text`
  part's `part.type` already was: it must equal the real `1.17.18`
  `"step-finish"` value exactly, so a missing, wrongly-typed, or lookalike
  value (e.g. the sibling `"step-start"`) fails closed instead of that
  event's `messageID` being trusted for terminality anyway.

## [0.1.1] - 2026-09-17

### Fixed

- Provider-overload misclassification (issue #80, PR #81): OpenCode
  `1.17.18` can emit a top-level `error` event whose `error.data.message`
  carries a serialized JSON provider payload; a `503`
  `provider_overloaded` Nvidia/OpenRouter overload in that shape was
  previously misclassified as `PROCESS_ERROR` with `provider_diagnostic =
  null` instead of the retryable `PROVIDER_ERROR`. The classifier now
  recognizes this exact, qualified transport shape via a narrow allowlist,
  preserves fail-closed behavior for malformed or lookalike variants
  outside the trusted structure, and leaves the coder's target-change
  retry suppression unchanged. The persisted provider diagnostic is also
  now surfaced in the final stderr summary when the last attempt has no
  terminal agent response.

## [0.1.0] - 2026-09-17

Initial implementation, delivered across milestones M01-M15 (15 merged
milestone PRs, #60-#74, 2026-09-13 through 2026-09-17).

### Added

- `opencode-tools run --workspace <path> --target <path-or-.> --issue <N>
  [--config <path>]` CLI command (`python -m opencode_tools run ...` is
  equivalent), with `cli.py` as the single composition root that builds
  adapters and injects them as ports (M14, PR #73).
- A Python-owned run lifecycle -- state machine, provider retry policy,
  subprocess timeouts, Git safety checks, target locking, and structured
  logging -- that drives three project-local OpenCode agents (`architect`,
  `coder`, `reviewer`) through exactly one GitHub issue per invocation.
  There is no fourth "orchestrator" agent; the agents only reason about the
  issue and write code.
- TOML configuration, `version = 1`, with a closed schema (`[execution]`,
  `[provider_retry]`, `[runtime]`, `[github.targets]`) and explicit
  `--config` -> conventional `<workspace>/opencode-tools.toml` -> built-in
  defaults precedence. No section accepts a model ID or provider-specific
  option for any agent.
- Provider retry: up to `max_attempts = 3` attempts, `initial_delay_seconds
  = 2`, `multiplier = 2.0`, capped at `max_delay_seconds = 30`. Only
  `PROVIDER_ERROR` outcomes are retryable, and a coder's provider retry is
  suppressed once the target has changed; review cycles (`CHANGES_REQUIRED`)
  are a separate counter, capped by `max_review_cycles = 3`.
- Structured, private run artifacts: an atomically written `run.json`
  (same-directory temp file, `fsync`, `os.replace`) plus append-only,
  line-framed per-role/cycle/attempt logs, created under
  `<runtime.root>/runs/<run-id>/` with directory mode `0700` and file mode
  `0600`.
- Exactly one `FINAL_STATUS: APPROVED` or `FINAL_STATUS: FAILED` line on
  stdout after a run initializes, with a non-interactive summary (run ID,
  last phase, terminal outcome, artifact path, preserved changes grouped by
  staged/unstaged/untracked, and any persisted error detail) on stderr, and
  stable exit codes: `0` approved, `2` usage error, `10`
  configuration/preflight failure, `20` provider/process/timeout/protocol/
  agent-reported/review-exhaustion failure or a handled interruption, `30`
  Git safety failure, `40` logging/persistence failure, `130` interrupted
  before a run could initialize.
- Zero Python runtime dependencies; `git`, the GitHub CLI (`gh`), and
  `opencode` are the only external executables shelled out to.

### Git safety

- A run is rejected before anything else happens unless `--target` resolves
  to the top level of a non-bare Git working tree, on an attached branch,
  with a clean index and working tree (no staged, unstaged, or non-ignored
  untracked changes).
- The `git-state-v1` content-sensitive fingerprint hashes tracked/untracked
  file content read from disk (not Git's object store), so a second edit to
  an already-`M` file changes the fingerprint; any instability, permission
  error, escaping path, or ambiguous probe is classified `INDETERMINATE`
  rather than guessed `SAFE`.
- Branch/HEAD drift is unsafe for every role; a fingerprint delta is safe
  only for the coder, and postflight is checked against the last accepted
  checkpoint on every path, including failures.
- The program never runs a mutating Git or GitHub command (no commit, push,
  reset, checkout, stash, branch, PR, issue edit/close, or comment) and
  never mutates GitHub; whatever the coder changes -- complete or partial --
  is always left uncommitted in the target.
- Per-target mutual exclusion via an advisory `fcntl.flock(LOCK_EX |
  LOCK_NB)` lease keyed on the target's absolute Git directory, so a
  different `runtime.root` cannot bypass it; a process group whose
  termination cannot be confirmed is quarantined via an atomically written
  `quarantine-v1.json` before the lease is released.

### Qualified

- **macOS/Linux POSIX platform baseline** (M15-02, issue #58, 2026-09-16):
  the full quality gate and deterministic acceptance suite were qualified
  independently on macOS (Darwin 25.6.0) and Linux (Debian GNU/Linux 13
  "trixie" under Docker Desktop's LinuxKit VM kernel), each repeated 3x --
  `python3.13 -m pytest` 1230 passed on both platforms, `ruff check .` all
  checks passed on both, `ruff format --check .` clean on both, and `mypy
  --strict src tests` succeeded on 60 source files on both. Windows
  (native or a Windows-mounted WSL filesystem) remains unsupported.
- **OpenCode `1.17.18`** (M15-03, issue #59, 2026-09-17): moved from
  *candidate* to *supported* -- the only version the exact-version registry
  now enables -- after two independent, opt-in live smoke runs
  (`tests/integration/test_opencode_1_17_18_smoke.py -m live`) against a
  disposable private GitHub repository both `PASSED`, with an identical
  evidence shape: exact version `1.17.18` with no nearby-version fallback;
  the real project-local `architect`/`coder`/`reviewer` agent identities
  verified with zero fallback to a default agent, model, or provider; three
  distinct `session_id`s, one per role; `git_safety_status: SAFE`
  throughout with the disposable target's remote `HEAD`, local `HEAD`, and
  commit count unchanged; and `final_status: APPROVED` /
  `terminal_outcome: SUCCEEDED` on both runs. This live evidence is
  macOS-only; the deterministic QG and acceptance suite above were
  qualified separately on both macOS and Linux.
