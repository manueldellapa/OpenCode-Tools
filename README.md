# OpenCode Tools

## Quality gate

Run the four quality checks from the repository root:

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```
