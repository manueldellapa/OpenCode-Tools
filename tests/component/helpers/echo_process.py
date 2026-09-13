"""A deterministic local child used by `test_process_runner.py`.

Standard library only, no package imports: it must run standalone under any
Python 3 interpreter reachable via an absolute path, exactly like a real
OpenCode or Git invocation.

Behavior, all driven by CLI flags so one helper covers every M05-0x scenario:
- always echoes stdin verbatim onto stdout (proves stdin is delivered
  out-of-band from argv/environment);
- `--print-cwd` additionally prints the process's current working directory
  on its own stdout line;
- `--echo-env NAME` (repeatable) additionally prints the value of
  environment variable `NAME` (or an empty line if unset), one per line, in
  the order given;
- `--stderr TEXT` writes `TEXT` to stderr;
- `--large N` writes `N` bytes to *both* stdout and stderr, interleaved in
  small chunks, to exceed the OS pipe buffer and force genuinely concurrent
  draining (M05-02);
- `--fork-hold SECONDS` forks a grandchild that inherits this process's
  stdout/stderr file descriptors and *stays in the same process group*
  (plain `fork()`, no `setsid()`), sleeping for `SECONDS` before exiting,
  while this process exits immediately -- simulating a descendant that
  keeps a pipe open after the direct child has already exited (M05-02);
- `--fork-and-sleep SECONDS` forks a grandchild that stays in the same
  process group and sleeps `SECONDS`, while *this* process also sleeps
  `SECONDS` instead of exiting -- so both must overrun the caller's
  deadline and both must receive a process-group signal to be reaped
  together (M05-03's "descendants get the escalation too" scenario);
- `--detach-fork-hold SECONDS` forks a grandchild that calls `os.setsid()`
  first, escaping this process's group, and sleeps `SECONDS`, while *this*
  process also sleeps `SECONDS` -- so a process-group signal reaps this
  process but can never reach the escaped grandchild, which keeps a pipe
  open past any bound: the realistic "unconfirmed termination" case
  (M05-03, System Design SS10.2);
- `--ignore-sigterm` installs a `SIGTERM` handler that does nothing, so only
  `SIGKILL` can end this process (M05-03's TERM-resistant scenario);
- `--sleep SECONDS` blocks for `SECONDS` before exiting normally -- used to
  simulate a child that overruns its deadline;
- `--exit-code N` exits with status `N` (default 0).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-cwd", action="store_true")
    parser.add_argument("--echo-env", action="append", default=[])
    parser.add_argument("--stderr", default=None)
    parser.add_argument("--large", type=int, default=None)
    parser.add_argument("--fork-hold", type=float, default=None)
    parser.add_argument("--fork-and-sleep", type=float, default=None)
    parser.add_argument("--detach-fork-hold", type=float, default=None)
    parser.add_argument("--ignore-sigterm", action="store_true")
    parser.add_argument("--sleep", type=float, default=None)
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args(argv)

    if args.ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    stdin_bytes = sys.stdin.buffer.read()
    if stdin_bytes:
        sys.stdout.buffer.write(stdin_bytes)

    if args.print_cwd:
        print(os.getcwd())
    for name in args.echo_env:
        print(os.environ.get(name, ""))

    if args.large is not None:
        block = b"x" * 4096
        remaining = args.large
        while remaining > 0:
            chunk = block if remaining >= len(block) else block[:remaining]
            sys.stdout.buffer.write(chunk)
            sys.stderr.buffer.write(chunk)
            remaining -= len(chunk)

    sys.stdout.flush()

    if args.stderr is not None:
        print(args.stderr, file=sys.stderr)
    sys.stderr.flush()

    if args.fork_hold is not None:
        pid = os.fork()
        if pid == 0:
            time.sleep(args.fork_hold)
            os._exit(0)
        os._exit(int(args.exit_code))

    if args.fork_and_sleep is not None:
        pid = os.fork()
        if pid == 0:
            time.sleep(args.fork_and_sleep)
            os._exit(0)
        time.sleep(args.fork_and_sleep)
        os._exit(int(args.exit_code))

    if args.detach_fork_hold is not None:
        pid = os.fork()
        if pid == 0:
            os.setsid()
            time.sleep(args.detach_fork_hold)
            os._exit(0)
        time.sleep(args.detach_fork_hold)
        os._exit(int(args.exit_code))

    if args.sleep is not None:
        time.sleep(args.sleep)

    return int(args.exit_code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
