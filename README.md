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
uv run cellrt trace yaw_mismatch/C/0/a1 --out runs/yaw.json  # the run as a trace for Perfetto
```

Events go to `runs/cellrt.sqlite` by default (`--db` to change).

`cellrt trace` writes Chrome trace event JSON (stdout without `--out`); open it in
<https://ui.perfetto.dev> or `chrome://tracing`. It has spans for the run, each skill run and
each planner call (with the model's answer, tokens and latency when a model planner was asked),
the Monitor's verdicts, recoveries and faults as instants, and tared force and depth as counters
(at most 2,000 points each). Marks are timed by wall time, so a model call shows its real cost;
`--clock sim` times them by the run's clock instead. Each mark has both clocks in its args.

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

The Phase 3 scenarios (`eval/scenarios/phase3.yaml`, ADR 019) add a second UNKNOWN,
`damaged_port`, that no skill can fix: the right decision is to stop early. Both of the file's
faults are designed by us. `--planner` (on `eval` and `run`) chooses what setting C asks:
`rule`, the default, or `fixed`, which retries and then aborts on its last planner call without
reading the evidence. stderr names the planner, since the table's C row does not:

```sh
uv run cellrt eval --scenarios eval/scenarios/phase3.yaml --settings B,C --planner fixed
```

`--planner llm` asks a model on NVIDIA NIM (ADR 017; Nemotron 3.5 Lightning by default,
`--model` to change). It needs the optional `llm` extra and a key in `NVIDIA_API_KEY`; the
base install, the tests and CI need neither, and no network. Before an eval starts, it checks
that setting C's units × the planner budget per task fit `--max-model-calls` (default 100), and
refuses otherwise; stderr reports the calls made. `cellrt smoke-llm` sends one fixed failure to
the model and prints how it answered, as JSON:

```sh
uv sync --extra llm
export NVIDIA_API_KEY=...   # never in a file of the repo
uv run cellrt smoke-llm
uv run cellrt eval --scenarios eval/scenarios/phase3.yaml --settings C --seeds 3 --planner llm
```

`cellrt agent-report` puts the planner variants side by side, one event store per variant
(`--db LABEL=PATH`; `C-llm#1`, `C-llm#2`, ... are repeats, pooled into one row with the spread
of their success rates). Per variant: success, the sim time and skills a failed run spends
after its first UNKNOWN, planner calls and decisions, invalid answers, timeouts, and the wall
latency and tokens of model calls. It refuses a store holding more than one setting or
configuration, the same store given twice, and repeats that asked different planners:

```sh
uv run cellrt agent-report --db B=runs/b.sqlite --db C-rule=runs/rule.sqlite \
  --db "C-llm#1=runs/llm1.sqlite" --db "C-llm#2=runs/llm2.sqlite"
```

The first run with a real model, and what it did and did not show, is in
[`docs/phase3-llm-results.md`](docs/phase3-llm-results.md).

`eval` can be killed at any point (a preempted VM) and run again with the same command: a unit
(scenario, setting, seed) that already finished under the same configuration is skipped, and
one that was interrupted runs again from the start as its next attempt (`.../a2`). Interrupted
attempts stay in the store and `cellrt replay` still prints them; the table uses finished runs
only, so a resumed eval prints the same table as one that was never interrupted. Change the
code under `cellrt/` or `adapters/`, `uv.lock` or a parameter, and every affected unit runs
again (the configuration hash covers them all). A unit interrupted 3 times under the same configuration is given up on and
reported on stderr, so a unit that crashes every time cannot block the rest.

A small eval (every scenario × A, B, C × 3 seeds, k 2, about 8 s) is the behavioural
regression gate: CI checks that it still gives the table recorded in
`eval/baselines/phase0-small.json`, and a mismatch lists every differing cell. Counts must match
exactly; rates and means may differ by at most 1e-9. When a change in behaviour is intended,
regenerate the file and label the pull request `baseline-update` (CI fails a baseline change
without that label). A failing check marks the pull request red but does not block the merge,
because the repository has no branch protection. The owner's Claude Code setup also holds an
agent's commit to the baseline, the comparison (`cellrt/eval/baseline.py`) or its tests until
the owner approves it (`.claude/one-way-doors`); nothing enforces that for other contributors:

```sh
uv run cellrt baseline eval/baselines/phase0-small.json            # check
uv run cellrt baseline eval/baselines/phase0-small.json --write    # regenerate (A,B,C, 3 seeds, k 2)
```

## The AIC cell (Phase 1, in progress)

`adapters/aic/` runs the same runtime as an `aic_model` policy in the AIC simulation: launch
the model with `policy:=adapters.aic.RuntimePolicy`, with cell-runtime installed in its
environment. Each goal (one task) becomes one run in the store named by `CELLRT_STORE`
(default `runs/aic.sqlite`): the setting `CELLRT_SETTING` names (A, B or C; default A), seed
0, and one scenario per goal,
`<session>/goal<N>/<task id>`, where `CELLRT_SESSION` names the engine config (default `aic`):
the engine sends no trial id, and every sample-config trial's task is `task_1`. Running the same
config again gives new attempts of the same scenarios, so set a new `CELLRT_SESSION` for every
other engine config: two configs under one session would share scenario ids and their runs
could not be told apart. Goal N matches the config's N-th task only when the engine sent every
goal before it (a trial it skips shifts the later ones). `cellrt replay` reads those
runs like any other.

By default (setting A) the policy runs CheatCode's two phases split into skills: `approach`, `align`
(down to z_offset 0.01 with CheatCode's XY correction), `insert_scripted` (on to z_offset
-0.015, ending as soon as the plug rests on its seat) and `verify` (5 s settle, then the
tip's height and offset from ground truth, against the seated band measured in the #28 sim
run); `retract` is there for recovery. z_offset is CheatCode's height above the port; where
the plug tip actually ends depends on CheatCode's tip-offset quirk
(`adapters/aic/geometry.py`): in #28 it seated two of the three plugs during align. Skill
starts, ends and progress (at most once per sim second) go out as `InsertCable` feedback.
`CELLRT_SETTING=B` or `C` runs the mock's recovery trees on these skills, with `retract` and a
wait for the F/T sensor as helpers. A recovery's slow variant runs approach, align and insert
at half speed. After a retract, align and insert go on down from the retracted pose, taken from
where the arm is measured, not where it was last sent: a plug pressed on something holds the
TCP far above its target (the stiffness is 90 N/m, so 10 N is 111 mm). If the retract left the
descent below z_offset 0.01, align first comes back up to it. `retract`
always rises 5 mm: the mock's retract height above the port does not carry over, since the
port frame sits at a different depth for each port type. Setting C asks the planner
`CELLRT_PLANNER` names (`rule`, `fixed` or `llm`; default `rule`; `llm` reads its key from
`NVIDIA_API_KEY` only). The executor runs in `live` mode (ADR 020): the sim does not stop
while a planner thinks, so the arm holds, the world steps and the Monitor keeps watching. A
bad value in either variable refuses every goal, before the arm moves.
Before the tree starts, a goal waits up to 10 s of sim time for the ground-truth frames and a
fresh wrench, as CheatCode waits for TF (ADR 011).
Everything except `RuntimePolicy.py` and `ros_port.py` is ROS-free and is tested on any OS
against a fake node (`tests/aic_fakes.py`); those two files run only where ROS 2 and AIC are
installed, and their tests are skipped elsewhere.

The skills read plug and port poses from the sim's ground-truth TF (launch the sim with
`ground_truth:=true`; every run records `geometry_source: ground_truth`). That is for local
development only: the AIC submission environment blocks those topics (`docs/access_control.md`
in the AIC repository).
