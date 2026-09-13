# OpenCode Tools

## Setup

Use Python 3.13 or newer. From the repository root and inside an active virtual
environment, install the declared development dependency group:

```bash
python3.13 -m pip install --group dev
```

Local v0.1 artifacts belong under `.opencode-tools/`, which this repository
ignores. The Python program does not add or correct ignore rules automatically.

## Quality gate

Run the four quality checks from the repository root:

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```
