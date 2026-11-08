# Phase 0 results

What the Phase 0 eval shows, how to reproduce it, and what it does not show.

## What this measures

Phase 0 runs on a mock world we designed. The faults are built so that a Monitor rule can detect
each one and a recovery can fix it, so the numbers below show **orchestration behaviour**:
detection, preemption, bounded recovery, stopping when budgets run out, and replay. They do
**not** show what recovery is worth. That is measured in the AIC sim in Phase 2 (#13), and if B
and C do not improve there, the report will say so.

"A < B ≤ C" is an observation, not a pass condition: tuning the mock until B wins would prove
nothing.

## Reproduce

```sh
uv run cellrt eval --scenarios eval/scenarios/phase0.yaml --settings A,B,C --seeds 20 --k 5
```

7 scenarios × 3 settings × 20 seeds = 420 runs. Measured on Windows 11 (Python 3.12, a laptop):

- one uninterrupted eval: **45 s** wall time; the event store is **34 MB** (`runs/cellrt.sqlite`);
- running the same command again: 1 s (`ran 0 units, 420 already finished`).

The mock runs in simulated time, so the same command prints the same table on any machine.

## The table

```
setting  runs  success  pass^5  recovery  unsafe  >20N_s  steps  duration_s  planner/task  accuracy
A         140    14.3%   14.3%      0.0%       0    0.07    2.6         5.8          0.00    100.0%
B         140    65.7%   45.4%     60.0%       0    0.07    5.5        11.2          0.00    100.0%
C         140    80.0%   59.7%     76.7%       0    0.07    5.8        11.7          0.14    100.0%

truth \ verdict  FORCE_LIMIT  INSERT_STUCK  POSE_ERROR  SENSOR_DROPOUT  TIMEOUT  UNKNOWN  missed
FORCE_LIMIT               60             0           0               0        0        0       0
INSERT_STUCK               0           106           0               0        0        0       0
POSE_ERROR                 0             0         110               0        0        0       0
SENSOR_DROPOUT             0             0           0              60        0        0       0
TIMEOUT                    0             0           0               0       88        0       0
UNKNOWN                    0             0           0               0        0       60       0

wins over A (A failed, this one succeeded; each needs a recovery or planner call):
  B: 72 won, 0 unexplained
  C: 92 won, 0 unexplained
```

Successes per scenario (out of 20 seeds), read from the same event store:

| scenario | A | B | C | why |
|---|---|---|---|---|
| baseline | 20 | 20 | 20 | no fault |
| controller_stall | 0 | 6 | 6 | B retries once; a stall (3–12 s) that outlasts both attempts' timeouts still fails |
| obstacle | 0 | 14 | 14 | blocks 1–3 insertions; B retries at most twice |
| pose_shift | 0 | 12 | 12 | shifts on 1–3 descents; B re-aligns at most twice |
| sensor_dropout | 0 | 20 | 20 | every dropout (0.5–1.0 s) ends within the 2 s wait |
| tight_fit | 0 | 20 | 20 | half speed keeps the force under 20 N for every drawn stiffness |
| yaw_mismatch | 0 | 0 | 20 | UNKNOWN to the Monitor; only C's planner switches to `insert_act` |

## Which numbers come from the mock

Each number follows from parameters we chose, not from the runtime:

- **Fault persistence** (`attempts`, `duration_s` in `eval/scenarios/phase0.yaml`) against the
  **recovery budgets** (`RecoveryConfig`, ADR 007) decides B's success in `obstacle`,
  `pose_shift` and `controller_stall`. Draw persistence from a wider range and B drops; raise
  the budgets and it rises.
- **The speed–force model** (`MockParams`) makes half speed enough for every `tight_fit` seed.
- **The dropout range** (0.5–1.0 s) is shorter than the 2 s `wait_sensor`, so every dropout
  recovers.
- **Classification accuracy of 100 %** holds because each fault was designed to match exactly
  one Monitor rule (ADR 005). It shows that the rules and the attribution work, not that the
  Monitor would classify real failures this well.
- **Baseline 100 %**, not the ~95 % the plan expected: in 300 baseline seeds there was no failure
  and no Monitor false alarm. The mock's noise (0.05 mm XY, 0.2 N force) is far below its
  thresholds (1 mm hole tolerance), and the mock was not changed to produce failures (that
  would only tune a number). The nominal failure rate is measured in the sim in Phase 2.
- **Unsafe 0, mean 0.07 s over 20 N**: `tight_fit` passes 20 N briefly before the Monitor stops
  it, well inside the 1 s AIC budget.

## How fairness is checked

`tests/test_fairness.py` runs every Phase 0 scenario in A, B and C and reads the event log:

- **Same problem:** for each (scenario, seed), the injected faults (`fault_injected`: kind, label,
  drawn parameters) are identical in A, B and C, and so is everything the world did and the
  Monitor saw, up to and including the first verdict. The settings diverge only where recovery
  starts.
- **Same judge:** `run_started.config_parts` records a hash per configuration part. Scenario,
  world, Monitor, skills and code are equal across settings; besides `setting` itself, only
  `recovery` (none in A) and
  `planner` (C only) differ.
- **Every gain explained:** each (scenario, seed) that B or C wins and A loses has a
  `recovery_started` or `planner_called` in the winning run's log. The eval prints this check
  under every table (`wins over A`), so it holds for the full 20-seed run, not only for the
  test's seeds.

The fairness tests were checked against mutations that key the fault or world randomness by
setting, or give A a different Monitor threshold: each mutation makes them fail.

## Phase 0 completion criteria (`docs/PHASE0_KICKOFF.md`)

| criterion | evidence |
|---|---|
| `pytest -q` passes | 351 tests; CI on every push |
| eval runs in one go and prints the table | above; `tests/test_eval.py` |
| A, B, C face identical faults, difference only from recovery | `tests/test_fairness.py` |
| one Monitor, one set of thresholds | `config_parts` check in `tests/test_fairness.py` |
| every extra success has a recovery or planner decision | `wins over A` line; `tests/test_fairness.py` |
| report states what depends on the mock | this file; the `note:` lines under every table |
| no ROS import in the core | `tests/test_ros_boundary.py` (AST scan) and ruff `banned-api` |
| LF line endings | `.gitattributes` (`* text=auto eol=lf`) |
| CI on ubuntu and windows | `.github/workflows/ci.yml` |
| kill and rerun gives the same table | `tests/test_resume.py` (hard kill in a subprocess) |
| README "How to run" | `README.md` |
