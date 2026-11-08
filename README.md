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

Run one scenario on the mock world and replay it from its event log:

```sh
uv run cellrt run --scenario baseline --setting A --seed 0   # prints outcome and run id
uv run cellrt run --scenario pose_shift --setting B --seed 0 # B recovers known failures
uv run cellrt run --scenario yaw_mismatch --setting C --seed 0 # C asks the planner about UNKNOWN
uv run cellrt replay baseline/A/0/a1                         # every step; add --signals for samples
```

Events go to `runs/cellrt.sqlite` by default (`--db` to change). `cellrt eval` arrives with the
Phase 0 eval tickets (#1).
