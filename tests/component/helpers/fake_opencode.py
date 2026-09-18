#!/usr/bin/env python3.13
"""A deterministic fake OpenCode `1.17.18` CLI used by M07 component tests.

Standard library only, no package imports: like `echo_process.py` for M05,
it must run standalone under any Python 3 interpreter reachable via an
absolute path, exactly like a real `opencode` binary resolved from `PATH`.

Unlike `echo_process.py`, this helper's argv dispatch mirrors the *real*
OpenCode CLI surface `opencode_tools.opencode` builds (`--version`,
`run --help`, `debug config`, `debug agent <role>`,
`run --agent <role> --format json --dir <path>`,
`export <session-id> --sanitize`), so component tests exercise the
adapter's actual command construction end to end. Canned content, exit
codes, and stderr text are supplied out of band via environment variables
(inherited from the parent test process, exactly like `SubprocessRunner`
inherits and merges `os.environ`) so one static script can play every M07
scenario:

- `FAKE_OPENCODE_VERSION`: stdout for `--version` (default "1.17.18").
- `FAKE_OPENCODE_RUN_HELP`: stdout for `run --help` (default advertises
  `--agent`/`--format json`/`--dir`).
- `FAKE_OPENCODE_DEBUG_CONFIG_FILE`: path whose bytes are echoed for
  `debug config`.
- `FAKE_OPENCODE_DEBUG_AGENT_<ROLE>_FILE`: path echoed for
  `debug agent <role>` (`<ROLE>` is the upper-cased role token, e.g.
  `ARCHITECT`).
- `FAKE_OPENCODE_RUN_OUTPUT_FILE`: path echoed for `run ...`.
- `FAKE_OPENCODE_EXPORT_FILE`: path echoed for `export ... --sanitize`.
- `FAKE_OPENCODE_EXIT_CODE`: overrides the exit code for any call
  (default 0).
- `FAKE_OPENCODE_STDERR`: extra text written to stderr for any call.
- `FAKE_OPENCODE_CALL_LOG_FILE`: path this process appends one line of
  space-joined argv to, so tests can assert call count/order/content.
- `FAKE_OPENCODE_CONTEXT_LOG_FILE`: optional JSONL log containing argv,
  cwd, and OPENCODE_CONFIG_DIR for assertions about role-specific context.
- `FAKE_OPENCODE_TARGET_DEBUG_CONFIG_FILE` and
  `FAKE_OPENCODE_TARGET_DEBUG_AGENT_<ROLE>_FILE`: optional overrides used
  when OPENCODE_CONFIG_DIR is present, so tests can make the nested-target
  control plane differ from the workspace control plane.
- `FAKE_OPENCODE_SLEEP_SECONDS`: blocks for this many seconds before
  responding to any call, for exercising a deadline miss.

Any call for which the relevant environment variable is unset prints
nothing on stdout and exits 0, which is a valid "empty" default for tests
that only care about a different call in the same sequence.

For a scenario that drives more than one logical invocation across
different agents (architect, then coder, then reviewer -- one `run`/
`export` pair per role), each new process only sees the same static
`FAKE_OPENCODE_RUN_OUTPUT_FILE`/`FAKE_OPENCODE_EXPORT_FILE`. Two optional,
additive variables let a test serve a distinct file per call instead,
without disturbing any scenario that only ever sets the static ones:

- `FAKE_OPENCODE_RUN_OUTPUT_FILES`/`FAKE_OPENCODE_EXPORT_FILES`: an
  `os.pathsep`-joined list consumed one entry per successive `run`/
  `export` call (the last entry repeats once exhausted).
- `FAKE_OPENCODE_RUN_OUTPUT_INDEX_FILE`/`FAKE_OPENCODE_EXPORT_INDEX_FILE`:
  a scratch file this process uses to remember which entry is next, since
  each call is a fresh process with no other shared state.
"""

from __future__ import annotations

import json
import os
import sys
import time

_DEFAULT_RUN_HELP = (
    "Usage: opencode run [--agent <name>] [--format json] [--dir <path>]\n"
)


def _log_call(argv: list[str]) -> None:
    log_path = os.environ.get("FAKE_OPENCODE_CALL_LOG_FILE")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(" ".join(argv) + "\n")

    context_log_path = os.environ.get("FAKE_OPENCODE_CONTEXT_LOG_FILE")
    if context_log_path:
        with open(context_log_path, "a", encoding="utf-8") as context_log:
            context_log.write(
                json.dumps(
                    {
                        "argv": argv,
                        "cwd": os.getcwd(),
                        "config_dir": os.environ.get("OPENCODE_CONFIG_DIR"),
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def _contextual_debug_file(base_env: str, *, target_env: str) -> str | None:
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        target_file = os.environ.get(target_env)
        if target_file:
            return target_file
    return os.environ.get(base_env)


def _echo_file(path: str) -> None:
    with open(path, "rb") as source:
        sys.stdout.buffer.write(source.read())


def _next_sequential_file(
    *, list_env: str, index_env: str, fallback_env: str
) -> str | None:
    files_raw = os.environ.get(list_env)
    if not files_raw:
        return os.environ.get(fallback_env)

    files = files_raw.split(os.pathsep)
    index_path = os.environ.get(index_env)
    index = 0
    if index_path and os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as index_file:
            index = int(index_file.read().strip() or "0")
    if index_path:
        with open(index_path, "w", encoding="utf-8") as index_file:
            index_file.write(str(index + 1))

    return files[min(index, len(files) - 1)]


def main(argv: list[str]) -> int:
    _log_call(argv)
    sys.stdin.buffer.read()

    sleep_seconds = os.environ.get("FAKE_OPENCODE_SLEEP_SECONDS")
    if sleep_seconds:
        time.sleep(float(sleep_seconds))

    if argv == ["--version"]:
        sys.stdout.write(os.environ.get("FAKE_OPENCODE_VERSION", "1.17.18") + "\n")
    elif argv[:2] == ["run", "--help"]:
        sys.stdout.write(os.environ.get("FAKE_OPENCODE_RUN_HELP", _DEFAULT_RUN_HELP))
    elif argv == ["debug", "config"]:
        config_file = _contextual_debug_file(
            "FAKE_OPENCODE_DEBUG_CONFIG_FILE",
            target_env="FAKE_OPENCODE_TARGET_DEBUG_CONFIG_FILE",
        )
        if config_file:
            _echo_file(config_file)
    elif len(argv) == 3 and argv[0] == "debug" and argv[1] == "agent":
        role = argv[2].upper()
        agent_file = _contextual_debug_file(
            f"FAKE_OPENCODE_DEBUG_AGENT_{role}_FILE",
            target_env=f"FAKE_OPENCODE_TARGET_DEBUG_AGENT_{role}_FILE",
        )
        if agent_file:
            _echo_file(agent_file)
    elif argv and argv[0] == "run":
        run_file = _next_sequential_file(
            list_env="FAKE_OPENCODE_RUN_OUTPUT_FILES",
            index_env="FAKE_OPENCODE_RUN_OUTPUT_INDEX_FILE",
            fallback_env="FAKE_OPENCODE_RUN_OUTPUT_FILE",
        )
        if run_file:
            _echo_file(run_file)
    elif len(argv) == 3 and argv[0] == "export" and argv[2] == "--sanitize":
        export_file = _next_sequential_file(
            list_env="FAKE_OPENCODE_EXPORT_FILES",
            index_env="FAKE_OPENCODE_EXPORT_INDEX_FILE",
            fallback_env="FAKE_OPENCODE_EXPORT_FILE",
        )
        if export_file:
            _echo_file(export_file)

    stderr_text = os.environ.get("FAKE_OPENCODE_STDERR")
    if stderr_text:
        sys.stderr.write(stderr_text)

    sys.stdout.flush()
    sys.stderr.flush()
    return int(os.environ.get("FAKE_OPENCODE_EXIT_CODE", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
