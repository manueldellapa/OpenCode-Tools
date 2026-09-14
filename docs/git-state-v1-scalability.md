# `git-state-v1` scalability qualification

This document records the M09-06 gate evidence: whether the `git-state-v1`
content-sensitive fingerprint (System Design SS11.2, ADR-003) completes within
`utility_timeout_seconds` on a large repository, per FR-051, NFR-005, NFR-008,
AC-028, and AC-036. It is an implementation-status record, not a planning
document: precedence for requirements still runs through the PRD, System
Design, and ADRs listed in `CLAUDE.md`. The canonical sources fix no corpus
size or additional SLO; this qualification registers the corpus actually used
and concludes against the configured timeout, inventing no new threshold.

## Outcome: **PASS**

The measured corpus completed the full checkpoint capture (`capture_git_state`,
which includes `git-state-v1` hashing plus M09-03's whole-snapshot resample)
in under one second on every repetition -- roughly 35-40x headroom under the
30-second default `utility_timeout_seconds`
(`_DEFAULT_UTILITY_TIMEOUT_SECONDS`, `src/opencode_tools/config.py`). No
increase to the default timeout is warranted by this evidence.

## Corpus

- 5,000 tracked regular files (`_QUALIFICATION_FILE_COUNT`,
  `tests/component/test_git_repository.py`), arranged as 50 directories of
  100 files each, every file a few bytes of unique, non-sensitive placeholder
  text (`"content {dir}-{file}\n"`). All 5,000 files are committed (no staged,
  unstaged, or untracked entries at measurement time), so the corpus exercises
  the tracked-list content-hashing path `git-state-v1` is defined over.
- This is a synthetic corpus generated at test time, not a checked-in fixture
  or any real project's repository; nothing about it is sensitive.

## Environment

| Field | Value |
|---|---|
| OS | macOS-26.6.2-arm64-arm-64bit-Mach-O (Apple Silicon) |
| Python | 3.13.15 |
| Git | 2.54.0 (Apple Git-157) |
| `utility_timeout_seconds` measured against | 30.0 (the configured default) |
| Repetitions | 5 (ad hoc measurement); the committed regression test
  (`test_git_state_v1_large_repository_qualification`) runs 3 |

## Observed durations (`capture_git_state`, full checkpoint capture)

| Repetition | Duration (s) |
|---|---|
| 1 | 0.755 |
| 2 | 0.810 |
| 3 | 0.876 |
| 4 | 0.777 |
| 5 | 0.720 |

The digest (`GitState.fingerprint`) was byte-for-byte identical across all
five repetitions (`fb47b9b45c40...`), confirming determinism/repeatability
for an unchanged corpus (AC-036).

## Fail-closed behavior when the deadline is exceeded

`test_git_state_v1_exceeded_deadline_on_a_large_repository_is_indeterminate`
(`tests/component/test_git_repository.py`) runs the same large-repository
capture with `utility_timeout_seconds=0.001`, forcing every underlying probe
past its deadline. The result is `GitSafetyStatus.INDETERMINATE` with
`state.fingerprint is None` -- never a fallback to porcelain-only hashing
(explicitly out of scope, per ADR-003 and M09-06's own "fuori scope": no
degradation to porcelain, no new algorithm, no reduction of content-sensitive
coverage). This reuses M09-03's already-existing bounded retry and
`INDETERMINATE` classification; M09-06 changes no runtime behavior, only adds
evidence and a regression test for it at this corpus size.

## Regression coverage

- `tests/component/test_git_repository.py::test_git_state_v1_large_repository_qualification`
  -- repeatable digest and completion within `utility_timeout_seconds`, on
  every CI run, at the corpus size described above.
- `tests/component/test_git_repository.py::test_git_state_v1_exceeded_deadline_on_a_large_repository_is_indeterminate`
  -- fail-closed `INDETERMINATE`, never a fallback, when the deadline is
  exceeded on the same corpus.

## Reproducing this measurement

The committed tests above are the durable regression guard. To reproduce the
ad hoc timing numbers in this document, run the qualification test with
output capture disabled:

```bash
python3.13 -m pytest tests/component/test_git_repository.py::test_git_state_v1_large_repository_qualification -s
```

## If a future, larger corpus exceeds the default

Per M09-06's scope, remediation is limited to raising `utility_timeout_seconds`
within its existing approved config range (`1`-`300` seconds,
`ExecutionConfig` in `src/opencode_tools/domain.py`) and re-running this
qualification with the new corpus and duration evidence appended below;
degrading to porcelain-only hashing, changing the `git-state-v1` framing, or
narrowing content-sensitive coverage are not acceptable remediations and
would require a new ADR.
