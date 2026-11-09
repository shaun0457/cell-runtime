# 019: Phase 3 scenarios: an UNKNOWN no skill can fix, and a fixed policy to compare with

Date: 2026-11-09
Status: accepted

**This decision changes what eval results mean** (it adds scenarios whose right answers we chose,
and a second planner for setting C, so a C result now depends on which planner ran).

## Context

Setting C asks a planner about failures the Monitor classifies as UNKNOWN (ADR 006, ADR 008).
The mock world has one such fault, `yaw_mismatch`, with one right answer: switch to
`insert_act`, which twists the plug free. The rule planner hard-codes that answer. So C − B on
Phase 0's scenarios shows that the plumbing works. It cannot show that a planner reads the
evidence: a policy that always switches to `insert_act` would score the same.

Phase 3 puts a model in the planner's place (#44). To measure it, the eval needs:
- a second UNKNOWN whose right answer is different, and whose evidence shows it;
- a simple policy with the same budgets, so the model is compared with more than a rule planner
  that already knows the answer.

Constraints:
- Phase 0's scenarios, metrics and committed baseline (`eval/baselines/phase0-small.json`) do not
  change. CI checks the baseline.
- A fault acts in the world and is seen only through signals (ADR 005). Its label is for the
  referee, never for the runtime (ADR 015, ground-truth roles; planned, not yet merged).
- The Monitor's rules do not change. UNKNOWN stays "stalled during insertion, and no known rule
  explains it" (ADR 006).
- The budgets stay: 2 planner calls and 4 recoveries per task (ADR 007, ADR 008).

## Options

### 1. What the second UNKNOWN looks like

#### Option A: a damaged port that nothing frees
The plug stops at a shallow depth under low force and low torque. Every insert skill stalls
there, on every attempt. The stall rule classifies it UNKNOWN.
- Pros:
  - Its right answer differs from the yaw mismatch's: stop early.
  - Its evidence differs too: near-zero torque and low force, against high torque and moderate
    force. A planner that reads the evidence can tell the two apart.
  - Small: one fault kind, modelled like the yaw wedge.
- Cons:
  - We shaped it to fit the stall rule, as we did the yaw mismatch.
  - "Stop early" is built in: no action helps, so any planner that tries something loses time.

#### Option B: a second UNKNOWN that a different action fixes
- Pros: rewards choosing the right action, not only choosing to stop.
- Cons:
  - Each scenario still has one right answer that a rule can hard-code.
  - It tests fixing, not knowing when to give up.
  - The mock has three actions (switch, retry, abort); a new fix needs a new skill.

#### Option C: one signature, fixable or not depending on a drawn parameter
- Pros: closest to real triage: the evidence decides.
- Cons:
  - The evidence must carry the parameter, so a rule can read it again.
  - Harder to explain and to tune: the ranges decide who wins.

### 2. Where the Phase 3 scenarios live

#### Option A: a separate file, `eval/scenarios/phase3.yaml`
- Pros:
  - Phase 0's file, table and baseline do not change.
  - The file is small (3 scenarios), so a model eval is quick to repeat.
- Cons: `baseline` and `yaw_mismatch` are copies, and copies can drift.

#### Option B: extend `phase0.yaml`
- Pros: one file; every eval covers every fault.
- Cons: Phase 0's table and committed baseline change; every Phase 3 eval also runs the five
  known-class scenarios, which never reach a planner.

### 3. The policy a model is compared with

#### Option A: the rule planner only
- Pros: nothing new.
- Cons: it knows the yaw answer by name. It is a ceiling on yaw, not a baseline.

#### Option B: a fixed retry-then-abort policy
It never reads the evidence: it retries the failed skill, and aborts on its last planner call.
- Pros: the same budgets; the simplest thing a cell could do.
- Cons: it can never win the yaw mismatch (it never switches skill). What it wastes on the
  damaged port (one retry) is set by our budget.

#### Option C: abort at the first call
- Pros: the least time wasted on the damaged port.
- Cons: it never recovers anything, so it adds little next to B, which recovers no UNKNOWN either.

### 4. How the fixed policy knows which call it is on

#### Option A: from the context (`planner_calls_left`), with no state
- Pros:
  - One instance serves every run of an eval and answers the same way each time.
  - It has no settings, so its type, which `config_hash` records (ADR 008), describes it fully.
- Cons: the rule is "retry until the last call". That equals "retry once, then abort" only with
  the default budget of 2.

#### Option B: a counter inside the planner
- Pros: reads the spec word for word at any budget.
- Cons: state across runs, so it must be reset per task; a shared instance gives answers that
  depend on run order.

#### Option C: a budget setting in the planner, to work out the call number
- Pros: "retry once" at any budget.
- Cons: `config_hash` records only the planner's type, so two budget settings would share a
  hash and a resumed eval would mix their results. It also repeats `RecoveryConfig.planner_budget`.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option 1A, Option 2A, Option 3B, Option 4A.

- **`damaged_port`**, a new mock fault labelled UNKNOWN. Its ranges, drawn per seed:
  - `stall_depth_mm` 0.5–1.5: where the plug stops;
  - `force_n` 2–4: below the Monitor's 10 N stuck threshold, so it is not INSERT_STUCK;
  - `torque_nm` 0–0.05: below `insert_act`'s 0.1 Nm twist threshold, so `insert_act` does not
    twist.
  - Nothing clears it: not twisting, not pulling out and pushing in again.
- **`eval/scenarios/phase3.yaml`** holds `baseline`, `yaw_mismatch` and `damaged_port`. The
  first two are Phase 0's, unchanged; a test checks they stay equal. `phase0.yaml`, the metrics
  and the baseline do not change.
- **`FixedPlanner`**, behind the same `Planner` protocol as the rule planner. It retries while
  more than one planner call and more than one task recovery are left, and aborts otherwise,
  with a fixed reason: it stops by its own decision before a budget runs out. With the default
  budgets: retry on the first call, abort on the second.
- **`--planner rule|fixed`** on `cellrt eval` and `cellrt run`. The default is `rule`; only
  setting C uses it. Names map to planners in one table (`PLANNERS` in `cellrt/cli.py`), where
  #49 adds `llm`.
- **Both scenarios are designed by us, and so are their right answers.** `yaw_mismatch` is won by
  switching to `insert_act`. `damaged_port` cannot be won: the best a planner can do is stop at
  its first call. Results on this file say so.

What each policy does, with the default budgets:

| Scenario | B | C, rule planner | C, fixed planner | Best in C |
|---|---|---|---|---|
| `yaw_mismatch` | failed | switch to `insert_act`: success | retry, then abort | success after 1 call |
| `damaged_port` | failed | switch, then retry: failed, planner budget used up | retry, then abort | abort at the first call |

## Why

- **The eval has to tell reading from guessing.** With one UNKNOWN, "always switch to
  `insert_act`" looks perfect. With two UNKNOWNs whose right answers conflict, no policy that
  ignores the evidence wins both. "Always switch" wastes time on the damaged port; "always stop"
  loses the yaw mismatch. A planner that reads torque and force can do well on both.
- **The evidence is there.** The context the planner gets holds the Monitor's evidence: torque,
  force and progress. The two faults differ in it, so the comparison does not ask a model for
  something it cannot see.
- **A separate file protects the regression gate.** Phase 0's table and baseline guard Phase 0's
  behaviour. Phase 3 work must not move them.
- **The fixed policy is the bar a model must clear.** It costs nothing and has the same budgets.
  If a model does no better than "retry once, then stop", it is not worth its latency and cost.
  The rule planner stays as the other reference: it knows the yaw answer, so it is a ceiling on
  yaw, not a baseline.
- **Counting from the context** keeps the planner stateless and its config hash honest.
- **The weakness to name first:** we built both faults and chose their right answers. A result on
  this file shows whether a planner uses evidence it is given, on two failures built to need it.
  It does not show how a planner does on failures we did not design. That is measured later, in
  the AIC sim.

## Consequences

- **This decision changes what eval results mean.** On `phase3.yaml` the C row depends on the
  planner. The row does not name it, so `cellrt eval` prints it on stderr. The run's
  `config_hash` records the planner's type, so a resumed eval never mixes two planners' results.
- 20 seeds, settings B and C (`cellrt eval --scenarios eval/scenarios/phase3.yaml
  --settings B,C --seeds 20 --k 5`):
  - B: 33.3 % success (the baseline only);
  - C, rule planner: 66.7 % (baseline and yaw mismatch), 1.00 planner calls per task;
  - C, fixed planner: 33.3 %, 1.33 planner calls per task, mean duration 8.0 s against B's 7.2 s.
- The table's notes still say "Phase 0 runs on a mock world we designed". They are Phase 0's
  notes, printed for any file; changing them would change Phase 0's output. The agent report in
  #44 gets its own.
- A fault kind that only Phase 3 uses is listed in `PHASE3_ONLY_KINDS` in `tests/test_eval.py`.
  The test checks that `phase0.yaml` covers every other kind once, and `phase3.yaml` these.
- Tests pin the design: the damaged port's force stays below the stuck threshold and its torque
  below the twist threshold. Changing a threshold or a range fails a test, because it would
  change what the scenario tests.
- To revisit:
  - Once a planner can escalate (#48, ADR 018), the best answer on the damaged port may be to
    escalate, not abort. The fixed policy keeps `abort`.
  - Option 1C, one signature with a drawn fix, once a model planner runs.
