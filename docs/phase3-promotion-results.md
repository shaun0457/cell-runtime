# Phase 3 results: two recurring UNKNOWNs promoted to Monitor rules (T3)

What happened when the two UNKNOWNs of the phase 3 scenarios were turned into Monitor rules with
a recovery plan each: found in a store, checked by replay (ADR 022), approved, and run in the
eval (ADR 021). Run on 2026-11-10, in the mock.

**Short answer: with the two rules, setting C makes no planner call on these faults, and B and
C-fixed reach what C-rule reached (66.7%).** C-rule's success rate does not change; what changes
is that it never asks the planner, and that it stops the unfixable fault at its first stall
instead of after its whole planner budget. Each rule names its failure a median 0.50 s before
the stall rule would have.

Read this with four limits in mind:

- **The scenarios are ours, and so are the rules.** `eval/scenarios/phase3.yaml` (ADR 019) holds
  two UNKNOWN faults built so that their right answers differ. The two rules below were written
  by Claude, the coding assistant, from the evidence `cellrt promote group` printed; they did
  not come from the proposer (T4), which asks a model through the rule language. The gates
  checked them; they did not find them.
- **The approval is a stand-in.** Both rules were approved as `Claude` into `runs/`, for this
  measurement. Nothing is committed under `eval/promoted/`: adopting a rule is Shaun's call.
- **The held-out seeds are a weak test.** The rules were validated on seeds 0-4 and the eval ran
  seeds 0-9. Seeds 5-9 gave the same results, but they draw the fault parameters from the same
  ranges in `phase3.yaml` (a yaw fault's torque from 0.4-0.8 Nm, a damaged port's force from
  2.0-4.0 N and torque under 0.05 Nm), and the thresholds clear those ranges. This says little
  about faults outside them.
- **One rule could be wrong in the sim.** `PORT_BLOCKED` reads "pressing at a few newtons, no
  torque, no progress". In the mock a plug that makes no progress is never seated. In the Phase 1
  sim run (#28), a seated plug pressed at 12-13 N with no progress was called `INSERT_STUCK`, a
  known class that comes before any promoted rule; `insert_scripted` now ends at the seat.
  Whether a seated plug pressed below 10 N would match `PORT_BLOCKED` has not been measured.

## Setup

| | |
|---|---|
| Code | branch `phase3/promotion`, at the commit that adds this file |
| Scenarios | `eval/scenarios/phase3.yaml`: `baseline`, `yaw_mismatch` (fixable: switch to `insert_act`), `damaged_port` (unfixable: stop early) |
| Survey store | settings A, B and C, seeds 0-4: 45 runs, 40 recorded verdicts |
| Eval | each variant: 3 scenarios x seeds 0-9 = 30 runs; the #52 variants that need no model |
| Model calls | none. C-llm is not rerun: with both rules adopted, setting C does not reach the planner on these scenarios, so a rerun would make no model call |

## Commands

From the repository root. The candidate files are below.

```sh
S=eval/scenarios/phase3.yaml
R=runs/promotion
ROT="insert_scripted/insert_stalled force <10 N torque >=0.1 Nm"
BLK="insert_scripted/insert_stalled force <10 N torque <0.1 Nm"
uv run cellrt eval --scenarios $S --seeds 5 --k 1 --settings A,B,C --db $R/survey.sqlite
uv run cellrt promote group --db $R/survey.sqlite
uv run cellrt promote validate $R/plug_rotated.json --group "$ROT" --db $R/survey.sqlite \
  --out $R/plug_rotated-report.json
uv run cellrt promote validate $R/port_blocked.json --group "$BLK" --db $R/survey.sqlite \
  --out $R/port_blocked-report.json
uv run cellrt promote approve $R/plug_rotated-report.json --by Claude --dir $R/approved
uv run cellrt promote approve $R/port_blocked-report.json --by Claude --dir $R/approved
for when in before after; do
  P=""; [ $when = after ] && P="--promoted $R/approved"
  uv run cellrt eval --scenarios $S --seeds 10 --k 1 --settings B --db $R/B-$when.sqlite $P
  uv run cellrt eval --scenarios $S --seeds 10 --k 1 --settings C --planner rule \
    --db $R/C-rule-$when.sqlite $P
  uv run cellrt eval --scenarios $S --seeds 10 --k 1 --settings C --planner fixed \
    --db $R/C-fixed-$when.sqlite $P
done
uv run cellrt agent-report \
  --db B=$R/B-before.sqlite --db C-rule=$R/C-rule-before.sqlite \
  --db C-fixed=$R/C-fixed-before.sqlite --db B-promoted=$R/B-after.sqlite \
  --db C-rule-promoted=$R/C-rule-after.sqlite --db C-fixed-promoted=$R/C-fixed-after.sqlite
```

The per-scenario counts, the seed halves, the planner calls and how `damaged_port` ended come
from the stores, not from the report:

```sh
uv run python - <<'EOF'
from collections import Counter
from pathlib import Path
from cellrt.runtime.events import PlannerCalled
from cellrt.runtime.store import EventStore
for name in ["B", "C-rule", "C-fixed"]:
    for when in ["before", "after"]:
        with EventStore(Path(f"runs/promotion/{name}-{when}.sqlite")) as store:
            ok, ends, calls = Counter(), Counter(), 0
            for run in store.runs():
                events = store.events(run.run_id)
                end = events[-1].payload
                half = "0-4" if run.seed < 5 else "5-9"
                ok[f"{run.scenario_id} {half}"] += end.outcome == "success"
                calls += sum(isinstance(e.payload, PlannerCalled) for e in events)
                if run.scenario_id == "damaged_port":
                    ends[f"{end.outcome}: {end.reason}"] += 1
            print(name, when, dict(sorted(ok.items())), "planner calls", calls, dict(ends))
EOF
```

`$R/plug_rotated.json`:

```json
{"rule": {"name": "PLUG_ROTATED", "skill": "insert_scripted", "window_s": 0.5,
  "conditions": [{"field": "torque_nm", "op": ">=", "value": 0.3},
                 {"field": "progress_mm", "op": "<", "value": 0.2}]},
 "plan": {"action": "switch_skill", "skill": "insert_act"}}
```

`$R/port_blocked.json`:

```json
{"rule": {"name": "PORT_BLOCKED", "skill": "insert_scripted", "window_s": 0.5,
  "conditions": [{"field": "progress_mm", "op": "<", "value": 0.2},
                 {"field": "torque_nm", "op": "<", "value": 0.1},
                 {"field": "force_n", "op": ">", "value": 1.5}]},
 "plan": {"action": "escalate"}}
```

## The groups

`cellrt promote group` over the survey store:

```text
replayed 45 runs: ok 45
gate 0 passed: every recorded verdict reproduced (40 verdicts in 45 runs; 0 skill timeouts, decided by the clock)
UNKNOWN groups, at least 3 verdicts:
  15 verdicts in 15 runs  insert_scripted/insert_stalled force <10 N torque <0.1 Nm  (referee: damaged_port 15)
  15 verdicts in 15 runs  insert_scripted/insert_stalled force <10 N torque >=0.1 Nm  (referee: yaw_mismatch 15)
  10 verdicts in 5 runs  insert_act/insert_stalled force <10 N torque <0.1 Nm  (referee: damaged_port 5)
```

The signature (skill, rule, force band, torque band) separates the two faults on
`insert_scripted`: the referee labels, which grouping never reads, show one fault kind per group.
The third group is C's switch to `insert_act` on the damaged port. It needs no rule of its own:
once `PORT_BLOCKED` escalates, `insert_act` never runs on that fault.

## The gates

Both candidates passed every gate on the survey store:

| gate | PLUG_ROTATED | PORT_BLOCKED |
|---|---|---|
| 0, reproduction | 40 verdicts in 45 runs reproduced | the same |
| 1, coverage | first verdict in 15 of 15 windows of its group, a median 0.50 s before the recorded UNKNOWN | the same |
| 2, no false alarms | first verdict in 0 of 15 runs without an injected fault | the same |
| 3, no theft | 0 of 25 other windows with a verdict changed their first verdict | the same |

Each report notes that replay checks the rule, not the plan: the recorded runs never ran it.
The eval below is the first time either plan ran.

## Before and after

```text
variant           setting  runs  success  wasted_s  wasted_steps  calls/run  invalid
B                 B          30    33.3%      0.00           0.0       0.00        -
C-rule            C          30    66.7%      2.20           2.0       1.00     0.0%
C-fixed           C          30    33.3%      1.15           1.0       1.33     0.0%
B-promoted        B          30    66.7%      0.00           0.0       0.00        -
C-rule-promoted   C          30    66.7%      0.00           0.0       0.00        -
C-fixed-promoted  C          30    66.7%      0.00           0.0       0.00        -
```

(`cellrt agent-report`, the columns that are not empty. **wasted**: on runs that did not
succeed, the sim time and the skills started from the first UNKNOWN to the end.)

Successful runs, before -> after, next to #52's model:

| scenario | B | C-rule | C-fixed | C-llm (#52, 3 x 10, not rerun) |
|---|---|---|---|---|
| baseline | 10/10 -> 10/10 | 10/10 -> 10/10 | 10/10 -> 10/10 | 30/30 |
| yaw_mismatch (fixable) | 0/10 -> 10/10 | 10/10 -> 10/10 | 0/10 -> 10/10 | 26/30 |
| damaged_port (unfixable) | 0/10 -> 0/10 | 0/10 -> 0/10 | 0/10 -> 0/10 | 0/30 |

Seeds 0-4 and seeds 5-9 gave the same counts in every cell.

How `damaged_port` ended:

| variant | before | after |
|---|---|---|
| B | failed at the first UNKNOWN (B has no planner) | escalated at the first stall (`promoted PORT_BLOCKED: escalate`) |
| C-rule | planner budget used up, after a switch to `insert_act` and a retry (2 calls) | escalated at the first stall, no call |
| C-fixed | aborted by the fixed policy, after a retry (2 calls) | escalated at the first stall, no call |

Planner calls over the 30 runs: C-rule 30 -> 0, C-fixed 40 -> 0. Mean sim duration for C-rule:
9.0 s -> 7.9 s. No run in any variant had a force-limit or off-limit event.

## What this shows

- **The loop works end to end.** A recurring UNKNOWN in a store becomes a rule with a plan, the
  replay gates check the rule against every recorded run, an approval file pins it to the
  validated hash, and the eval runs its plan in B and C without the planner.
- **Promotion is worth model calls, not success on these faults.** C-rule already reached 66.7%
  on its own. What the rules removed is the planner call per failure: in #52 each call to the
  model cost a median 12.8 s of wall time and about 1,950 tokens. On a cell the arm holds for
  that time.
- **The unfixable fault is where the rule changes the decision.** In #52 no planner stopped on
  `damaged_port` because of what it saw: C-rule spent its budget, C-fixed aborted on schedule
  after a retry, and the model never stopped by choice. An approved rule stops at the first
  stall, because the approval fixed what that signature means. Here Claude approved it as a
  stand-in; in use, a person would.

## What this does not show

- That the rules are right outside the mock (see the limits at the top: a seated plug pressed
  below 10 N, above all).
- That a model can find such rules. T4 gives a model the group's evidence and the rule language;
  its proposals go through the same gates.
- How promotion behaves when a fault's signature drifts. The mock's faults do not drift.
