# OpenCode Tools

OpenCode Tools is a local, non-interactive Python 3.13+ command that drives
three project-local [OpenCode](https://opencode.ai) agents -- `architect`,
`coder`, `reviewer` -- through exactly one GitHub issue per invocation.
Python owns the whole lifecycle (state machine, retry, timeouts, Git safety,
locking, logging); the agents only reason about the issue and write code.
There is no fourth "orchestrator" agent.

**Status: v0.1, qualified.** The command, composition root, and terminal
rendering described below (milestones M01-M14) are implemented and covered
by the test suite. OpenCode `1.17.18` is qualified as the supported
version, and macOS and Linux on a local POSIX filesystem are the qualified
platform baseline -- see [docs/compatibility.md](docs/compatibility.md)
for the full qualification evidence.

## Requirements

- Python 3.13 or newer.
- `git`, the GitHub CLI (`gh`, already authenticated for the target
  repository's host), and `opencode` resolvable on `PATH`.
- macOS or Linux on a local POSIX filesystem. Windows (native or a
  Windows-mounted WSL filesystem) is not supported -- see
  [docs/compatibility.md](docs/compatibility.md).

The tool itself has zero Python runtime dependencies; `git`, `gh`, and
`opencode` are external executables it shells out to, never Python
packages.

## Setup

From the repository root, inside an active virtual environment, install the
declared development dependency group (needed to run the test suite and
quality gate; the tool itself needs nothing beyond the standard library):

```bash
python3.13 -m pip install --group dev
```

## Usage

```bash
opencode-tools run --workspace <path> --target <path-or-.> --issue <N> [--config <path>]
```

`python -m opencode_tools run ...` is equivalent (see `pyproject.toml`'s
`[project.scripts]` entry and `src/opencode_tools/__main__.py`).

- `--workspace`: the shared OpenCode workspace. Architect and reviewer run
  with this directory as their context; it also owns the project-local
  `.opencode/` control plane. Must already exist.
- `--target`: the real Git repository whose working-tree changes the coder
  may ultimately produce and the reviewer inspects, given as `.` or a path
  relative to `--workspace`. The coder never runs directly inside this
  repository; OpenCode-Tools creates an independent disposable local clone,
  runs the coder there, validates it, and promotes only the resulting
  working-tree delta. After
  resolving symlinks it must stay inside the workspace and be the top level
  of a non-bare Git working tree, on an attached branch, with a clean index
  and working tree (no staged, unstaged, or non-ignored untracked changes) --
  the run is rejected before anything else happens if it is not.
- `--issue`: a single positive GitHub issue number. There is no batch,
  range, or list form; exactly one issue per invocation.
- `--config`: an explicit `opencode-tools.toml` path. If omitted, the
  conventional `<workspace>/opencode-tools.toml` is used when present,
  otherwise built-in defaults apply.

Workspace, real target, and coder execution directory are deliberately
distinct concepts. Architect and reviewer use the workspace as their
OpenCode context. Each coder attempt receives a fresh independent clone in a
private temporary directory; that clone has no remotes, no alternate object
store, and no shared Git metadata with the real target. OpenCode Tools pins
`OPENCODE_CONFIG_DIR` to the workspace-owned `.opencode/` directory so the
reviewed coder definition remains effective inside the disposable clone.

Before promotion, OpenCode-Tools verifies that the clone still has the
sandbox baseline `HEAD`, that its `.git` directory is intact, and that the
real target's content-sensitive Git fingerprint did not change while the
coder was running. Only the sandbox working-tree delta is then applied to
the real target; refs, commits, tags, branches, remotes and Git metadata are
never promoted.

## Configuration

Configuration is TOML, `version = 1`, with a closed schema (an unknown key
anywhere is a config error). No section accepts a model ID or a
provider-specific option for any of the three agents -- OpenCode's own
configuration governs that, not this tool's.

```toml
version = 1

[execution]
opencode_timeout_seconds = 1800
utility_timeout_seconds = 30
termination_grace_seconds = 5
max_review_cycles = 3

[provider_retry]
max_attempts = 3
initial_delay_seconds = 2
multiplier = 2.0
max_delay_seconds = 30

[runtime]
root = ".opencode-tools"

# Only when the target's remote is not enough or must be constrained.
[github.targets."."]
repository = "github.com/example/backend"
```


`opencode_timeout_seconds` is a **hard safety ceiling**, not the expected way a
successful CODER run ends. Once the requested implementation is complete and all
required acceptance criteria have been verified, the CODER is expected to stop
exploratory work, summarize the required verification, and emit
`AGENT_STATUS: COMPLETED` immediately. It must not spend the remaining timeout
budget speculating about hidden tests, alternate layouts, optional tooling, or
unrelated variants. A failed or inconclusive optional diagnostic does not block
completion when the same required criterion has already been independently
verified by another valid method. Conversely, an unresolved required criterion
or failed required verification still requires `AGENT_STATUS: FAILED`.

Every field above is optional and shown at its own default; `[runtime]
root` defaults to `.opencode-tools` relative to the workspace and may be
relative or absolute. `[github.targets."<target>"]` is keyed by the
target's workspace-relative path (`"."` for the common case) and overrides
GitHub repository resolution for that target when the target's own Git
remotes are ambiguous or must be constrained (`repository`, `remote`, or
both).

Runtime artifacts for every run live under `<runtime.root>/runs/<run-id>/`
(default `<workspace>/.opencode-tools/runs/<run-id>/`). If that location
ends up inside a Git working tree, it must already be excluded by that
repository's own ignore rules before any run can start -- this repository's
own [.gitignore](.gitignore) already excludes `.opencode-tools/` for that
reason. The program never edits `.gitignore`, and never repairs a runtime
root's mode or ownership; see
[docs/security-and-privacy.md](docs/security-and-privacy.md).

## Output and exit codes

After a run has been initialized, exactly one line is printed to stdout:

```text
FINAL_STATUS: APPROVED
```

or

```text
FINAL_STATUS: FAILED
```

`APPROVED` is only possible after the reviewer's own `REVIEW_STATUS:
APPROVED` and a final Git postflight check that finds the target's branch
and `HEAD` unchanged from the run's baseline. Every other terminal outcome
is `FAILED`. Nothing else is ever printed to stdout; a concise,
non-interactive summary -- run ID, last phase, terminal outcome, artifact
path, the preserved changes grouped by staged/unstaged/untracked, and any
persisted error detail -- goes to stderr instead. A failure before a run
directory could exist (bad arguments, an invalid config, a dirty target, a
missing/incompatible tool) prints no `FINAL_STATUS` line and promises no
artifact.

| Exit code | Meaning |
|---:|---|
| 0 | `FINAL_STATUS: APPROVED` |
| 2 | Command-line usage error, before any run could start |
| 10 | Configuration or preflight failure (before or shortly after run init) |
| 20 | Provider, process, timeout, protocol, agent-reported, or review-exhaustion failure, or a handled interruption |
| 30 | Git safety failure (drift, or an indeterminate probe) |
| 40 | Logging/persistence failure -- the run artifact may be incomplete |
| 130 | Interrupted before a run could be initialized |

Only a technically valid coder attempt whose sandbox passes validation is
promoted to the real target. Those promoted changes are left uncommitted.
Partial changes from a provider/process/protocol failure remain confined to
the disposable clone and are discarded when that attempt ends. The program
may stage and create a temporary baseline commit **inside the disposable
clone only**; it never commits the real target. See
[docs/recovery.md](docs/recovery.md) for how to inspect and recover a run
manually.

## What this does not do

- No batch, range, resume, or `inspect-run`: one issue per invocation,
  start to finish, or nothing.
- No automatic retention, rotation, or pruning of `.opencode-tools/` runs;
  the user deletes old run directories manually.
- No model or provider selection from Python; that is entirely OpenCode's
  own configuration.
- No publication or real-target Git-history mutation: OpenCode-Tools never
  commits, branches, tags or pushes the real target and never mutates
  GitHub. Trusted internal Git commands are used only to build/validate the
  disposable clone and apply a working-tree patch to the real target.
- No general OS sandbox: issue #90 isolates the real target's Git metadata
  with an independent disposable clone, but this is not containment against
  an arbitrary hostile local process running with the same user credentials.
  See [docs/security-and-privacy.md](docs/security-and-privacy.md).

## Quality gate

Run the four quality checks from the repository root:

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

## Further reading

- [docs/security-and-privacy.md](docs/security-and-privacy.md) -- threat
  model, logged/not-logged data, filesystem privacy, redaction limits.
- [docs/recovery.md](docs/recovery.md) -- inspecting a finished or failed
  run, the target lock, and quarantine.
- [docs/compatibility.md](docs/compatibility.md) -- the qualified OpenCode
  version and platform baseline, and the qualification evidence behind
  each.
- [`tasks/prd-opencode-tools.md`](tasks/prd-opencode-tools.md),
  [`tasks/system-design-opencode-tools.md`](tasks/system-design-opencode-tools.md),
  and [`docs/adr/`](docs/adr/) -- the canonical requirements, design, and
  accepted decisions this implementation follows, highest precedence first.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for
the full text.
