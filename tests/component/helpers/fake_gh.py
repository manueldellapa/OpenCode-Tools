#!/usr/bin/env python3.13
"""A deterministic fake `gh` CLI used by M11-02 component tests.

Standard library only, no package imports: like `fake_git.py` and
`fake_opencode.py`, it must run standalone under any Python 3 interpreter
reachable via an absolute path, independent of the `opencode_tools`
package. Its argv dispatch mirrors the two calls `github.run_gh_preflight`
ever makes -- `gh --version` and `gh auth status --hostname <host>` -- so
component tests exercise the real bounded-subprocess path end to end.

Unlike `fake_opencode.py`, which shares one exit-code/stderr/sleep control
across every call it can receive, this helper's two calls need to be
controlled *independently*: a preflight always runs `--version` first and
only reaches `auth status` if that succeeds, so a test proving "auth status
fails" must still let `--version` succeed. Canned content, exit codes, and
per-call sleeps are therefore supplied out of band via distinct
`FAKE_GH_VERSION_*` / `FAKE_GH_AUTH_*` environment variables (inherited
from the parent test process, exactly like `SubprocessRunner` inherits and
merges `os.environ`):

- `FAKE_GH_VERSION_OUTPUT`: stdout for `--version` (default a realistic
  multi-line `gh` banner with version `2.40.1`).
- `FAKE_GH_VERSION_EXIT_CODE`: exit code for `--version` (default 0).
- `FAKE_GH_VERSION_SLEEP_SECONDS`: blocks for this many seconds before
  responding to `--version`, for exercising a deadline miss.
- `FAKE_GH_AUTH_EXIT_CODE`: exit code for `auth status --hostname <host>`
  (default 0).
- `FAKE_GH_AUTH_STDOUT`: stdout text for `auth status` (default empty).
- `FAKE_GH_AUTH_STDERR`: stderr text for `auth status` (default empty) --
  real `gh` prints its human-readable "not logged in"/"token expired" text
  here; `github.py` must never decode or persist it, only the process
  outcome, which is exactly what these tests assert on.
- `FAKE_GH_AUTH_SLEEP_SECONDS`: blocks for this many seconds before
  responding to `auth status`, for exercising a deadline miss.
- `FAKE_GH_CALL_LOG_FILE`: path this process appends one line of
  space-joined argv to, so tests can assert call count/order/content
  (e.g. that `--hostname` carries the expected host).

Any call for which the relevant environment variable is unset behaves as a
harmless, immediate, zero-exit success with empty output.
"""

from __future__ import annotations

import os
import sys
import time

_DEFAULT_VERSION_OUTPUT = (
    "gh version 2.40.1 (2023-12-13)\nhttps://github.com/cli/cli/releases/tag/v2.40.1\n"
)


def _log_call(argv: list[str]) -> None:
    log_path = os.environ.get("FAKE_GH_CALL_LOG_FILE")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(" ".join(argv) + "\n")


def _sleep_if_requested(env_var: str) -> None:
    sleep_seconds = os.environ.get(env_var)
    if sleep_seconds:
        time.sleep(float(sleep_seconds))


def main(argv: list[str]) -> int:
    _log_call(argv)
    sys.stdin.buffer.read()

    if argv == ["--version"]:
        _sleep_if_requested("FAKE_GH_VERSION_SLEEP_SECONDS")
        sys.stdout.write(
            os.environ.get("FAKE_GH_VERSION_OUTPUT", _DEFAULT_VERSION_OUTPUT)
        )
        sys.stdout.flush()
        return int(os.environ.get("FAKE_GH_VERSION_EXIT_CODE", "0"))

    if (
        len(argv) == 4
        and argv[0] == "auth"
        and argv[1] == "status"
        and argv[2] == "--hostname"
    ):
        _sleep_if_requested("FAKE_GH_AUTH_SLEEP_SECONDS")
        sys.stdout.write(os.environ.get("FAKE_GH_AUTH_STDOUT", ""))
        sys.stderr.write(os.environ.get("FAKE_GH_AUTH_STDERR", ""))
        sys.stdout.flush()
        sys.stderr.flush()
        return int(os.environ.get("FAKE_GH_AUTH_EXIT_CODE", "0"))

    # An unrecognized call shape is a bug in the test/adapter wiring, not a
    # scenario under test: fail loudly rather than silently succeeding.
    sys.stderr.write(f"fake_gh.py: unrecognized argv {argv!r}\n")
    sys.stderr.flush()
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
