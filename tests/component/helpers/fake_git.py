#!/usr/bin/env python3.13
"""A controllable fake `git` used only for AC-036 probe-failure tests.

Standard library only, no package imports: like `echo_process.py` and
`fake_opencode.py`, it must run standalone under any Python 3 interpreter
reachable via an absolute path, independent of the `opencode_tools`
package. It ignores argv entirely (`git_safety.py` always calls it with a
fixed, allowlisted `-C <target> <subcommand>` shape) and is driven purely by
`FAKE_GIT_*` environment variables, so a single probe call's outcome --
a timeout, a non-zero exit, or non-UTF-8/ambiguous stdout -- can be forced
without needing a real hung or corrupted repository.
"""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    sleep_seconds = os.environ.get("FAKE_GIT_SLEEP_SECONDS")
    if sleep_seconds:
        time.sleep(float(sleep_seconds))

    stdout_file = os.environ.get("FAKE_GIT_STDOUT_FILE")
    if stdout_file:
        with open(stdout_file, "rb") as handle:
            sys.stdout.buffer.write(handle.read())
    else:
        sys.stdout.write(os.environ.get("FAKE_GIT_STDOUT", ""))
    sys.stdout.flush()

    return int(os.environ.get("FAKE_GIT_EXIT_CODE", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
