# Changelog

All notable changes to this project are documented in this file. The format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- Closed a Git clean-filter/textconv remote command execution path in coder
  sandbox promotion (issue #110): a coder-writable `.gitattributes`,
  `.git/config`, or `.git/info/attributes` could previously make the
  orchestrator's own trusted `git add`/`git diff` invocations run an
  arbitrary command as the operator's user, before the coder's changes were
  even promoted. `promote_coder_changes` now refuses to stage or diff
  anything unless those three files are still byte-identical to the state
  captured right after the sandbox was created, and passes
  `--no-ext-diff --no-textconv` to the diff itself as defense in depth.

## [0.1.2] - 2026-09-22

### Security

- Protected the real target repository's Git metadata from destructive CODER
  commands (issue #90, PR #91). CODER execution now happens in a disposable
  sandbox and changes are promoted back only after the attempt succeeds and
  passes the existing fail-closed Git safety checks. A failed, timed-out, or
  unsafe attempt cannot replace or reinitialize the target's real `.git`
  metadata.
- Aligned the effective CODER command policy with the documented no-Git-mutation
  contract (issue #102, PR #106), denying canonical Git working-tree/history
  mutation commands even though general implementation shell access remains
  available.

### Fixed

- Corrected CODER context for nested targets (issue #88, PR #89): the CODER now
  runs with the resolved target as its OpenCode working directory while the
  workspace-owned control plane remains authoritative.
- Accepted structurally valid intermediate completed-text events from OpenCode
  1.17.18 instead of misclassifying them as multiple terminal responses
  (issue #85, PR #86), while preserving fail-closed lifecycle validation.
- Rejected an incomplete final OpenCode tool-call lifecycle as terminal
  (issue #87, PR #96), including blank/invalidated terminal text and malformed
  or trailing lifecycle activity.
- Added deterministic CODER completion discipline after all required acceptance
  criteria are implemented and verified (issue #95, PR #97), preventing
  optional exploration from consuming the remaining timeout budget.
- Added a fast no-op path for already-satisfied issues (issue #101, PR #105):
  the CODER verifies the acceptance criteria first and avoids speculative or
  formatting-only edits when no concrete gap exists.
- Preserved trusted diagnostics when post-run agent identity verification fails
  (issue #100, PR #104), so the resulting protocol failure remains
  reconstructible instead of collapsing to an opaque `PROTOCOL_ERROR`.
- Added matching ARCHITECT stopping discipline for already-satisfied work
  (issue #103, PR #107): once enough evidence exists for a safe handoff, the
  architect stops optional exploration and directs the CODER to verify before
  changing anything.

### Changed

- Simplified and deduplicated agent prompt contracts (issue #98, PR #99),
  keeping stable role policy in the static agent definitions while generated
  runtime prompts carry only run-specific context.
- Added a GitHub Actions pull-request quality gate (issue #83, PR #84) using
  Python 3.13 and the repository's canonical pytest, Ruff, formatting, and
  strict mypy checks.

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
