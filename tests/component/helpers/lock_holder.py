#!/usr/bin/env python3.13
"""A standalone process that `flock`s a given path and holds it, used only
to prove target-scoped lock contention and kernel release across genuinely
separate OS processes (M10-02).

Standard library only, no package imports: like `echo_process.py` and
`fake_git.py`, it simulates an independent process rather than standing in
for an external tool -- here, another concurrent `opencode-tools` run
contending for the same lock file.

Usage: lock_holder.py <lock_path> <ready_marker_path> [hold_seconds]

Opens (creating if needed) `lock_path`, mode `0600`, and acquires an
exclusive, non-blocking `flock` -- the exact same primitive
`locking.PosixTargetLeaseFactory.acquire` uses. Once held, touches
`ready_marker_path` so the test knows the lock is genuinely held by this
separate process, then sleeps for `hold_seconds` (default effectively
indefinite) before exiting normally and releasing the lock. The test
either waits it out or sends `SIGKILL` to simulate a crashed holder and
prove the kernel releases the lock regardless of how this process ends.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time


def main(argv: list[str]) -> int:
    lock_path, ready_path = argv[0], argv[1]
    hold_seconds = float(argv[2]) if len(argv) > 2 else 3600.0

    file_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    with open(ready_path, "w", encoding="utf-8") as marker:
        marker.write("ready\n")

    time.sleep(hold_seconds)
    os.close(file_descriptor)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
