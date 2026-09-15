# OpenCode Tools

OpenCode Tools is a local, non-interactive Python 3.13+ command that drives
three project-local [OpenCode](https://opencode.ai) agents -- `architect`,
`coder`, `reviewer` -- through exactly one GitHub issue per invocation.
Python owns the whole lifecycle (state machine, retry, timeouts, Git safety,
locking, logging); the agents only reason about the issue and write code.
There is no fourth "orchestrator" agent.

**Status: v0.1, pre-release.** The command, composition root, and terminal
rendering described below (milestones M01-M14) are implemented and covered
by the test suite. OpenCode `1.17.18` is still a *candidate*, not a
*supported*, version -- see [docs/compatibility.md](docs/compatibility.md)
for exactly what remains open before the M15 qualification gates pass.

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

- `--workspace`: the directory OpenCode runs in as its working context. Must
  already exist.
- `--target`: the Git repository the coder edits and the reviewer
  inspects, given as `.` or a path relative to `--workspace`. After
  resolving symlinks it must stay inside the workspace and be the top level
  of a non-bare Git working tree, on an attached branch, with a clean index
  and working tree (no staged, unstaged, or non-ignored untracked changes) --
  the run is rejected before anything else happens if it is not.
- `--issue`: a single positive GitHub issue number. There is no batch,
  range, or list form; exactly one issue per invocation.
- `--config`: an explicit `opencode-tools.toml` path. If omitted, the
  conventional `<workspace>/opencode-tools.toml` is used when present,
  otherwise built-in defaults apply.

Workspace and target are deliberately distinct: OpenCode is always invoked
with the workspace as its context directory, while every Git/diff operation
names the target explicitly. In the common case they are the same
directory (`--target .`); they can differ for a multi-repository workspace.

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

Whatever changes the coder made -- complete or partial -- are always left
uncommitted in the target, on success or on failure; the program never
commits, stages, resets, or cleans anything. See
[docs/recovery.md](docs/recovery.md) for how to inspect and recover a run
manually.

## What this does not do

- No batch, range, resume, or `inspect-run`: one issue per invocation,
  start to finish, or nothing.
- No automatic retention, rotation, or pruning of `.opencode-tools/` runs;
  the user deletes old run directories manually.
- No model or provider selection from Python; that is entirely OpenCode's
  own configuration.
- No Git or GitHub mutation of any kind (no commit, push, branch, PR,
  issue edit/close, or comment) and no automatic publication or sharing.
- No sandbox: the cooperative controls this tool applies are not
  containment against a hostile local process. See
  [docs/security-and-privacy.md](docs/security-and-privacy.md).

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
- [docs/compatibility.md](docs/compatibility.md) -- exact OpenCode version
  and platform baseline, and what is still open before v0.1 release.
- [`tasks/prd-opencode-tools.md`](tasks/prd-opencode-tools.md),
  [`tasks/system-design-opencode-tools.md`](tasks/system-design-opencode-tools.md),
  and [`docs/adr/`](docs/adr/) -- the canonical requirements, design, and
  accepted decisions this implementation follows, highest precedence first.
