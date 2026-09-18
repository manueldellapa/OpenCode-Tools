# Security and privacy

This document describes the threat model and privacy controls OpenCode
Tools v0.1 actually implements and tests, per ADR-010 (cooperative threat
model) and ADR-008 (workspace runtime and artifact privacy). It is an
implementation-status record, not a planning document: precedence for
requirements still runs through the PRD, System Design, and ADRs listed in
`CLAUDE.md`.

## Threat model

**Untrusted inputs:** the issue body, the target repository's contents,
filenames, agent handoff/feedback text, and process streams and provider
errors. None of these can redefine a role, a safety policy, or the agent
protocol; they are wrapped in trusted delimiter lines the agents are
instructed never to treat as instructions.

**Trusted control plane:** the installed OpenCode Tools code itself, the
already-validated configuration, the version-specific OpenCode adapter, the
state machine's transition table, and the three agents' verified
definitions and effective configuration. `git`, `gh`, and the local
`opencode` binary are operational dependencies assumed not to be
compromised.

v0.1 defends against mistakes, malformed output, cooperative prompt
injection, an OpenCode agent silently falling back to the wrong agent,
concurrent conformant runs on the same target, and observable Git mutation.

**v0.1 is not a general OS sandbox.** Issue #90 adds a narrow but concrete
containment boundary for the coder's Git metadata: every coder attempt runs
inside an independent disposable clone whose remotes are removed and whose
object store is not shared with the real target. Destructive Git operations
inside that clone therefore cannot destroy the target's real `.git`.

Residual risks still include a process with the same user permissions that
escapes OpenCode's directory boundary, a compromised
`git`/`gh`/`opencode` binary, credential misuse, or remote mutation
performed through another channel. See ADR-010 for the remaining threat
model.

## Controls actually applied

- Every subprocess uses structured argv with `shell=False`; the agent
  prompt is passed on stdin, never interpolated into a command line.
- The issue, coder report, and reviewer feedback are wrapped in
  `=====BEGIN/END UNTRUSTED <LABEL>=====` delimiters inside the prompt.
- Agent status/review markers are read only from the terminal assistant
  message of the trusted NDJSON transport; a marker quoted inside the issue,
  a prompt, tool output, or stderr is never accepted (agent protocol v1).
- The effective per-role permission matrix is verified before any role
  runs: architect and reviewer are read-only, the coder may edit the
  target's working tree only, and `ask`, `task`, Git mutation, and sharing
  are all denied for every role.
- `--auto`, `--share`, and `--model` are never passed to `opencode`, and an
  OpenCode configuration with automatic sharing enabled is rejected before
  any role runs.
- The three agents' effective configuration and the relevant OpenCode
  configuration are hashed into one control-plane digest at preflight; the
  same digest is re-checked, bounded, before every subsequent invocation.
  A drift -- including one caused by the coder editing OpenCode's own
  configuration when target and workspace coincide -- is a protocol error
  and stops the pipeline before the next role runs.
- The target's Git branch, `HEAD`, and a content-sensitive working-tree
  fingerprint are captured before and after every invocation.
- The coder never executes in the real target. OpenCode-Tools creates a
  private local clone with an independent `.git`, removes all remotes,
  mirrors the current target working-tree state through a temporary Git
  index, and runs OpenCode only inside that disposable clone.
- Before promotion, the sandbox `.git`, top-level and baseline `HEAD`
  must still be valid and the real target fingerprint must match the
  pre-attempt snapshot. Only the sandbox working-tree delta is then applied
  to the real target.
- Runs on the same target are serialized by a per-target lock (below); an
  unconfirmed termination quarantines the target rather than releasing it
  silently.

Python never mutates GitHub or the real target's history/refs. Trusted
internal Git mutation is now intentionally used **inside the disposable
sandbox only** to stage and create a temporary baseline commit, and
`git apply` is used to promote a validated working-tree patch into the
real target. The real target is never committed, reset, cleaned, branched
or pushed. Failed coder attempts are discarded with their sandbox; promoted
changes remain uncommitted. See [docs/recovery.md](recovery.md).

## What is logged, and what is not

| Recorded | Where | Sensitivity |
|---|---|---|
| Issue number, resolved repository identity, URL, title | `run.json` | may reveal a private project/issue |
| Workspace/target paths and a Git file inventory | `run.json` | local paths and source structure |
| Versions, statuses, durations, digests, sanitized errors | `run.json` | operational metadata |
| Agent stdout/stderr, test/tool output, OpenCode events | the attempt log for that invocation | can contain issue text, code, and printed secrets |
| Prompt, handoff, and reviewer feedback | not duplicated into `run.json`; may appear in the raw agent stream | sensitive |

**Deliberately never recorded:** the full process environment, tokens or
API keys, the credential store, raw `gh auth status` output, the raw
effective OpenCode configuration, the raw sanitized OpenCode export, the
prompt text inside a logged command, or any model/provider identifier.
Logged command strings use `<PROMPT_REDACTED>`; any credential embedded in
a URL is stripped before it is ever recorded.

OpenCode and the provider configured for it necessarily see the issue and
the target's code in order to do the work -- that is intrinsic to the
product and is not a transfer this tool controls or can prevent. This tool
only prevents *automatic* publication or sharing on top of that; it does
not set or enforce the configured provider's own retention policy. Decide
whether your provider and its policies are appropriate for a private
repository before running this tool against one.

OpenCode's own session storage (created by `opencode run` independently of
this tool) is separate from `.opencode-tools/`; this tool does not reuse,
publish, or delete it.

## Filesystem privacy

On POSIX, a newly created runtime root, its `runs/` directory, and every
individual run directory are created with mode `0700`; every file and
temporary file inside them, including `run.json` itself, is created with
mode `0600` -- never more permissive. An *existing* runtime root must
belong to the effective user, must not be a symlink, and must have every
group/other permission bit cleared; otherwise preflight fails closed
without silently repairing the mode or ownership for you.

`run.json` is written through a same-directory temporary file, `fsync`,
and an atomic `os.replace` -- never through an in-place truncate. If a
write ever fails, the previous valid `run.json` is left exactly as it was
(possibly still describing an earlier phase of the same run); only the
failed attempt's own temp file is best-effort removed. A logging failure
of any kind stops new invocations, is reported as `LOGGING_ERROR`, and
stderr explicitly says the artifact may be incomplete.

## Redaction limits

Raw attempt logs are classified sensitive **even with a restrictive
`0600` mode**: they capture whatever the agent process printed, including
tool and test output, and this tool cannot reliably redact a secret an
external process decides to print on its own. There is no regex-based
redaction pipeline in v0.1, and none is claimed -- an unverified redaction
claim would be worse than none. If you need that guarantee, treat every
attempt log as if it might contain a secret and handle it accordingly.

## Retention

v0.1 applies no retention, rotation, or automatic pruning of anything
under `.opencode-tools/`. A run directory stays on disk, in full, until
you delete it yourself, and may be picked up by your normal backup
process like any other local file. Reclaiming disk space, deciding how
long to keep a run, and removing it after troubleshooting are all your
responsibility, not this tool's.
