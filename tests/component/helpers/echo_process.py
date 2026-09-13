"""A deterministic local child used by `test_process_runner.py`.

Standard library only, no package imports: it must run standalone under any
Python 3 interpreter reachable via an absolute path, exactly like a real
OpenCode or Git invocation.

Behavior, all driven by CLI flags so one helper covers every M05-01 scenario:
- always echoes stdin verbatim onto stdout (proves stdin is delivered
  out-of-band from argv/environment);
- `--print-cwd` additionally prints the process's current working directory
  on its own stdout line;
- `--echo-env NAME` (repeatable) additionally prints the value of
  environment variable `NAME` (or an empty line if unset), one per line, in
  the order given;
- `--stderr TEXT` writes `TEXT` to stderr;
- `--exit-code N` exits with status `N` (default 0).
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-cwd", action="store_true")
    parser.add_argument("--echo-env", action="append", default=[])
    parser.add_argument("--stderr", default=None)
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args(argv)

    stdin_bytes = sys.stdin.buffer.read()
    if stdin_bytes:
        sys.stdout.buffer.write(stdin_bytes)

    if args.print_cwd:
        print(os.getcwd())
    for name in args.echo_env:
        print(os.environ.get(name, ""))

    sys.stdout.flush()

    if args.stderr is not None:
        print(args.stderr, file=sys.stderr)

    return int(args.exit_code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
