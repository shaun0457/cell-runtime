# Phase 3 in the AIC sim: settings A, B and C under a command dropout (#61)

The first run of the whole loop in Gazebo with a fault: the Monitor calls a dropped link
UNKNOWN, and settings A, B, C-rule and C-LLM each handle it their own way. One engine run per
setting, 18 tasks in all. This is a confirmation set, not a statistic: large-N numbers come from
the mock (`docs/phase3-llm-results.md`).

## What ran

| | |
|---|---|
| Where | GCP VM `cellrt-sim` (g2-standard-8, L4), runbook `docs/runbooks/phase1-sim-run.md` |
| AIC | `ghcr.io/intrinsic-dev/aic/aic_eval@sha256:9aa2ffdbb946d38edde1bac7b5f02a44cfbea26e3b04a9c74e09f14c97472923`, sample engine config: goal1 and goal2 SFP, goal3 SC |
| cell-runtime | `f60cc83` on `phase3/sim`; the clean run used `3438b9d` (#59, before the fault code; it injects nothing) |
| Geometry | ground-truth TF (oracle geometry): what the runtime reads about poses is perfect. The ground-truth roles ADR (015) is drafted but not on this branch yet |
| Fault | `CELLRT_FAULT={"kind": "command_dropout", "onset_height_mm": 20, "duration_s": 15}` (#60) on every goal: from the first sample where the plug tip is within 20 mm of the port, every motion command is dropped for 15 s of sim time. Ground-truth label UNKNOWN |
| Settings | clean A (no fault), A, B, C-rule, and C-LLM twice: `nvidia/nemotron-3.5-lightning-30b-a3b` on NVIDIA NIM, `max_tokens` 1024, deadline 30 s, live wait (ADR 020) |
| Model calls | 6: at most 2 per task (the planner budget), under the default cap of 100 per policy process |

## Results

Each cell is one task: the runtime's outcome, then the AIC scorer's verdict on the insertion
(75 is a full insertion). The two agree on all 18.

| Goal | clean A | A | B | C-rule | C-LLM #1 | C-LLM #2 |
|---|---|---|---|---|---|---|
| goal1 SFP | success, 75 | UNKNOWN, 0 | UNKNOWN, 0 | abort, 0 | **retry, success, 75** | not JSON, 0 |
| goal2 SFP | success, 75 | success, 75 | UNKNOWN, 0 | abort, 0 | escalate, 0 | timeout, 0 |
| goal3 SC | INSERT_STUCK, 0 | UNKNOWN, 0 | UNKNOWN, 0 | abort, 0 | not JSON, 0 | not JSON, 0 |

What happened, by setting:

- **The fault and the Monitor.** The dropout began during `align`, between 11.8 and 19.8 s of sim
  time. `align` ran its remaining targets into the void, `insert_scripted` started with the plug
  hanging above the port, and about 1 s later the Monitor called it UNKNOWN (`insert_stalled`:
  0.5-2.1 N, 0.01-0.33 Nm, at most 0.19 mm of progress in 1 s). That is the verdict #60 designed
  the fault to produce, in 14 of the 15 faulted tasks.
- **A.** Ends on the first failure, as designed. In goal2 the fault had no effect: the dropout
  began later (15.0 s), the plug seated anyway and `insert_scripted` and `verify` passed; the
  scorer agrees. A fault that changes nothing counts as "no effect", not as a missed detection.
- **B.** Has no recovery for UNKNOWN, so it ends at the same verdict as A. In goal2 the dropout
  began earlier (12.7 s) and the plug did not seat.
- **C-rule.** The rule planner switches to the other insert skill. The sim registry has only
  `insert_scripted` (ACT as `insert_act` is Phase 2), so it has nothing to switch to and aborts
  at once, in 0.05 s of sim time, every time.
- **C-LLM.** Six calls, one useful answer:
  - goal1 #1: the model answered `retry` after 10.65 s of wall time. The world kept running while
    it thought (3.03 s of sim time, ADR 020), and the link came back at 27.06 s, during the wait.
    The retried `insert_scripted` seated the plug in 0.3 s, `verify` passed, and the scorer gave
    75. It did not get there by the scripted descent (0.5 mm per 0.05 s step): the tip went from
    11.6 mm to -1.0 mm in three samples (0.15 s), because the descent resumed from the targets
    the dropout had swallowed, well below the arm (see Force).
  - goal2 #1: `escalate` to a human, with a reason that names the stall and guesses at a
    misalignment or an obstruction.
  - goal2 #2: cut at the 30 s deadline.
  - goal1 #2, goal3 #1 and #2: no answer it could read (`not_json`). Each stopped at all 1024
    completion tokens (`finish_reason` length) with no tool call; the likely cause, as for 17 of
    the 89 calls in #52, is that the model reasoned until the limit.
- **goal3 (SC).** Fails in every setting, also without a fault: the SC plug jams at the port
  entrance (#28). The dropout only changes which verdict ends it.

## What this shows and what it does not

- **The loop works end to end in the sim:** a fault the runtime cannot see, a Monitor verdict
  from signals alone, a model call while the arm holds and the sim runs on, a validated decision,
  a bounded recovery, and a scorer that agrees with the runtime's own outcome on every task.
- **The model's one success depended on timing, and on a skill defect.** `retry` worked in
  goal1 #1 because the link had come back by the time the answer arrived, and the retried
  descent then jumped about 12 mm in 0.15 s instead of stepping down (see Force). So it shows
  that the answer was validated and executed, not that a retry recovers a dropout cleanly. In
  goal3 the dropout began at about 19.5 s, so it was still active when the calls ended (29.6 s
  and 33.1 s): a retry would have stalled again. The model cannot know the link state; neither
  can the rule planner.
- **A deterministic answer may exist, for some of them.** The 14 dropout verdicts look alike:
  `insert_stalled` on the first second of `insert_scripted`, under 2.1 N, with no progress.
  "Wait for the link, then retry" is a hypothesis for the 9 on the SFP goals; the 5 on goal3
  would still meet the SC entrance jam. Finding a rule and checking it by replay is the job of
  UNKNOWN promotion (ADRs 021-022 on `phase3/promotion`); its replay is built for mock stores
  first, and 14 verdicts from one fault are too few to trust a rule on.
- **Not shown:** anything about rates. One run per cell, one engine config, one fault, oracle
  geometry, a fault we designed. The not-JSON answers are a token-limit setting (#52), not a
  property of the sim.

## Force

The scorer saw force over 20 N once, in goal1 C-LLM #1: 0.02 s with a peak of 86.39 N, under
its 1 s budget, no penalty. Our F/T stream counted the same 0.02 s over the limit at 28.82 s, in
the retried descent; our 20 Hz samples peaked at 14.2 N, so the spike fell between samples. That
is the known limit #60 recorded: while the link is down the descent's commanded target keeps
moving (`Descent.command` records each target whether or not it reached the arm), so the first
target after the dropout can be well below the arm. Here it was: the tip fell 12.6 mm in 0.15 s
into the port, which is where the spike and the seat both came from. Only a retract re-anchors
the descent on the measured pose; a retry does not. The Monitor's force rule did not fire,
correctly: 0.02 s is far below its 0.5 s trigger. Fixing the descent is open (PR review, F1).

## Media

Recorded in the VM from the three wrist cameras (headless Gazebo) and composed with the event
log as a caption panel, at 2x speed. Kept outside the repository until the project site is
reviewed:

- `c_llm_goal1_retry_hero.mp4` (12 s, 292 KB): the stall, the model thinking while the arm
  holds, the retry's jump seating the plug.
- `c_llm_goal1_full.mp4`, `a_goal1_full.mp4` (the same fault under A), `c_llm_goal2_escalate.mp4`.
- `trace_c_llm_goal1_wall.json`, `trace_c_llm_goal1_sim.json`: `cellrt trace` of goal1 C-LLM #1;
  on the wall clock the planner call fills 10.65 s of the run.

## Reproduce

The runbook's sections 4-6 and its section 10, with the policy settings from environment
variables (README, "The AIC cell"): `CELLRT_SETTING`, `CELLRT_PLANNER`, `CELLRT_FAULT`, and for
`llm` the key in `NVIDIA_API_KEY`. Session names were `s61clean` (clean), `s61` (A, B, C-rule) and
`s61llm` (C-LLM, attempts a1 and a2). Inspect a run from a copy of the store:

```bash
cellrt replay s61llm/goal1/task_1/C/0/a1 --db sim61-copy.sqlite
cellrt trace s61llm/goal1/task_1/C/0/a1 --db sim61-copy.sqlite --out trace.json
```
