# Recovery

OpenCode Tools never recovers a target automatically. This document
describes what is actually preserved after a run, how to inspect it, and
how to clear the two states -- an active lock and a quarantine -- that can
block a later run on the same target. It is an implementation-status
record, not a planning document: precedence for requirements still runs
through the PRD, System Design, and ADRs listed in `CLAUDE.md`.

## Nothing is committed, and nothing is undone

Whatever the coder changed -- complete or partial, on success or on
failure -- is always left uncommitted in the target's working tree. Python
never runs a mutating Git command against the target: no commit, stage,
reset, clean, checkout, stash, branch, or push, at any point in the
lifecycle, including after a failure. Postflight is a read-only check, not
a recovery step.

This means recovery is entirely manual, with your own `git` commands, in
your own time:

- `FINAL_STATUS: APPROVED` -- the reviewer approved the changes and the
  target's branch/`HEAD` never moved during the run; review the working
  tree with `git status`/`git diff` in the target and commit it yourself
  when you are satisfied.
- `FINAL_STATUS: FAILED` -- inspect the working tree the same way. There
  may be complete work, partial work, or no code change at all, depending
  on which phase the run reached; the stderr summary's `terminal outcome`
  and the run's `run.json` say which. Keep, discard (`git restore`/`git
  clean`, run by you, not by this tool), or continue from what is there --
  the tool takes no position on which is correct.

## Where to look

Every run's stderr summary (the lines after `FINAL_STATUS`) reports the
run ID, the terminal outcome, and the artifact path when one exists. That
path points at `<run-id>/run.json` under the runtime root (default
`<workspace>/.opencode-tools/runs/<run-id>/`), alongside one append-only
log file per agent invocation (`architect-provider-attempt-1.log`,
`coder-cycle-1-provider-attempt-1.log`, and so on -- one file per role,
review cycle when applicable, and provider attempt, never overwritten).
`run.json` is the single machine-readable source of truth for what
happened, in what order, and why; the attempt logs are diagnostic raw
output and are sensitive (see
[docs/security-and-privacy.md](security-and-privacy.md)).

A run that failed before it could even be initialized (a bad argument, an
invalid config, a dirty target, a missing or incompatible tool) leaves no
`run.json` and no run directory at all -- there is nothing under
`.opencode-tools/` to inspect for that attempt; the stderr diagnosis is
everything you get, and it is everything there is.

## The target lock

Every run acquires a non-blocking, advisory POSIX lock
(`fcntl.flock(LOCK_EX | LOCK_NB)`) on `target.lock`, under
`<git-dir>/opencode-tools/` for the target's checkout, held from just
before the baseline capture until after finalization. Two checkouts (or
Git worktrees) with distinct Git directories get distinct locks and can
run in parallel; the same checkout cannot.

If a run's own preflight reports the target is locked (`PREFLIGHT_ERROR`,
`locking.target_locked`), that means a run is genuinely holding it right
now. The correct response is to wait for that run to finish. **Do not
delete `target.lock` as a presumed-stale file:** its mere presence on disk
is not proof that a lock is still held (the kernel releases the lock when
the holding process exits or crashes, whether or not the file itself is
removed), and this tool deliberately implements no staleness heuristic
based on PID or file age -- there is no reliable way to guess a lock is
stale from outside the holding process, and PID reuse would make a
PID-based guess actively dangerous. A leftover `target.lock` file with no
process actually holding the OS-level lock is harmless and will simply be
re-acquired by the next run.

## Quarantine

If a run cannot confirm that a child process group it terminated is
actually gone (the bounded `terminate -> grace -> kill -> grace` sequence
completed without proof of death), it writes
`<git-dir>/opencode-tools/quarantine-v1.json` before releasing the lock.
A quarantined target **blocks every future run**, even once the OS-level
lock is free, until the quarantine file is gone.

Quarantine is never removed automatically -- not by PID check, not by
age, not by the next run's own preflight. Clearing it is a deliberate,
manual decision:

1. Confirm, yourself, that no process from the terminated run is still
   running (for example, `ps` for anything referencing the target or the
   run's OpenCode invocation).
2. Inspect the target's Git state and working tree (`git status`, `git
   log`, `git branch`) for anything unexpected the surviving process
   might have done.
3. Once satisfied, delete `quarantine-v1.json` yourself.

If the quarantine file itself could not be written (for example, a
filesystem error), the run's final status is still `FAILED` and stderr
says explicitly that future exclusion on that target is not guaranteed --
treat that target with extra caution until you have verified it by hand.

## What this does not do

There is no `resume`, no automatic retry of a failed run, no `inspect-run`
subcommand, and no cleanup command. Recovery is: read the stderr summary
and `run.json`, look at the target's working tree with your own Git
commands, and decide what to do with what is there. A future release may
add tooling for some of this; v0.1 does not promise it.
