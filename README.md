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

Events go to `runs/cellrt.sqlite` by default (`--db` to change).

Run every scenario × setting × seed and print the metrics table, read from the event log
(about 45 s for the defaults: 7 scenarios × A, B, C × 20 seeds):

```sh
uv run cellrt eval --scenarios eval/scenarios/phase0.yaml --settings A,B,C --seeds 20 --k 5
```

One row per setting: success rate, pass^k, recovery rate (runs with a Monitor verdict that end
in success), unsafe runs (time with tared force over 20 N, summed over the run, above 1 s, as in
the AIC penalty; read from the signals, not from the Monitor), mean time over 20 N, mean steps
and duration, planner calls per task, and Monitor classification accuracy. Below it, a confusion
matrix of verdicts against the injected faults, with false alarms (`none`) and misses. A rate
with nothing to measure is shown as `-`. Then the wins over A (runs A lost and another setting
won), each of which must show a recovery or planner call in its log, and notes on which numbers
depend on the mock. `docs/phase0-results.md` has the 20-seed results and what they do and do
not show.

`eval` can be killed at any point (a preempted VM) and run again with the same command: a unit
(scenario, setting, seed) that already finished under the same configuration is skipped, and
one that was interrupted runs again from the start as its next attempt (`.../a2`). Interrupted
attempts stay in the store and `cellrt replay` still prints them; the table uses finished runs
only, so a resumed eval prints the same table as one that was never interrupted. Change the
code under `cellrt/` or a parameter, and every affected unit runs again (the configuration hash
covers both). A unit interrupted 3 times under the same configuration is given up on and
reported on stderr, so a unit that crashes every time cannot block the rest.
