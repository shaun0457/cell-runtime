# cell-runtime

An agent runtime for a robotic work cell: it makes a cable-insertion robot know when it has
failed and recover, and it measures how much that recovery is worth. See `CLAUDE.md` for the
design and `docs/decisions/` for the decision records.

## How to run

Requires [uv](https://docs.astral.sh/uv/). It installs the pinned Python 3.12 and every
dependency from `uv.lock`.

```sh
uv sync                     # create .venv and install the locked environment
uv run pytest -q            # tests
uv run ruff check           # lint
uv run ruff format --check  # formatting
```

The runtime CLI (`cellrt run | eval | replay`) arrives with the Phase 0 tickets (#1).
