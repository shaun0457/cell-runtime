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
| Geometry | ground-truth TF (oracle geometry): what the runtime reads about poses is perfect. ADR 015 sets out what ground truth may be used for |
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
- **goal3 (SC).** Fails in every setting of this run, also without a fault: the SC plug jams at
  the port entrance (#28). The dropout only changes which verdict ends it. (In the reruns below,
  without a fault, it seated in two of three C runs after the F1 fix and in three of three with
  the 4096-token budget.)

## What this shows and what it does not

- **The loop works end to end in the sim:** a fault the runtime cannot see, a Monitor verdict
  from signals alone, a model call while the arm holds and the sim runs on, a validated decision,
  a bounded recovery, and a scorer that agrees with the runtime's own outcome on every task.
- **The model's one success depended on timing, and on a skill defect.** `retry` worked in
  goal1 #1 because the link had come back by the time the answer arrived, and the retried
  descent then jumped about 12 mm in 0.15 s instead of stepping down (see Force; fixed since,
  and in the rerun below a retry steps down). So it shows that the answer was validated and
  executed, not that a retry recovers a dropout cleanly. In
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
into the port, which is where the spike and the seat both came from. In this run only a retract
re-anchored the descent on the measured pose; a retry did not. The Monitor's force rule did not
fire, correctly: 0.02 s is far below its 0.5 s trigger.

Fixed after this run (PR review, F1) and rerun (next section): a descent run that starts after
a broken one (stopped by the Monitor, timed out, or after a retract) goes on from the measured
pose, its z_offset and its XY integrator, so a retry steps down from where the arm is. The
chosen fix does not cover a jump when the link comes back inside a run, or after a run that ran
to its end while its commands were lost (align, then insert_scripted). Covering it would take a
world that reports lost commands, which hands the skills knowledge of the fault, or a check in
the skills that the arm follows its targets at low force, which changes CheatCode's contact
phase.

## Rerun after the F1 fix

`618ae75` on `phase3/sim`, on 2026-11-10, with the same VM, image and engine config: a clean
setting A run, then C-LLM three times with #61's fault on the SFP goals only (`"goals": [1, 2]`;
goal3 jammed at the SC entrance in every setting of #61, so a fault there would test nothing
new). Six
model calls, at most 2 per task. The scorer agrees with the runtime on all 12 tasks.

| Goal | clean A | C-LLM #1 | C-LLM #2 | C-LLM #3 |
|---|---|---|---|---|
| goal1 SFP, dropout | success, 75 | not JSON, 0 | not JSON, 0 | not JSON, 0 |
| goal2 SFP, dropout | success, 75 | not JSON, 0 | **retry, success, 75** | not JSON, 0 |
| goal3 SC, no fault | INSERT_STUCK, 0 | INSERT_STUCK, 2 retract_realign, 0 | success, 75 | INSERT_STUCK, 1 retract_realign, success, 75 |

- **The retry steps down.** goal2 #2: the model answered `retry` after 6.5 s of wall time (1.95 s
  of sim time). The retried `insert_scripted` started at 27.40 s, 0.2 s before the link came back
  at 27.60 s, so its first four targets were lost: the case the fix does not cover. The plug
  held at 12.6 mm until then, then went down 0.11 to 0.60 mm per 50 ms sample and reached its
  seat (-0.97 mm) 1.65 s after the retry started, at most 3.2 N in our samples; the scorer saw
  no force over 20 N and gave 75. Before the fix (#61 goal1 #1, where the link was back before
  the retry started) a retry took the plug from 12.3 mm to -1.0 mm in 0.21 s (0.69, 3.85, 6.93
  and 1.86 mm per sample), 14.2 N in our samples and a spike of 86.4 N in the scorer's.
- **The model answered one call in six.** The other five stopped at all 1024 completion tokens
  without a tool call (`not_json`), as 3 of 6 did in #61 and 17 of 89 in #52: the token limit,
  not the sim. So this rerun checks the fix, on one retry, not the model.
- **Clean A is unchanged:** goal1 and goal2 seated (75 each), goal3 INSERT_STUCK at 13.3 N, as in
  #61's clean run.
- **goal3 ran setting C's tree without a fault**, so B's table handled it. The SC plug jammed
  at its entrance in two runs: in C-LLM #3 one `retract_realign` then seated it (scorer 75), in
  #1 two did not and the INSERT_STUCK budget of 2 ran out. In #2 it seated during align. One
  recovery that worked is one case, not a rate.

## Rerun with a 4096-token budget

`2d52365` on `feat/planner-settings`, on 2026-11-10, with the same VM, image, engine config and
fault as the F1 rerun (`"goals": [1, 2]`): C-LLM three times with `CELLRT_MAX_TOKENS=4096` and
`CELLRT_REASONING=default`, the setting the mock comparison chose
(`docs/phase3-llm-results.md`, "Token budget and reasoning"). Three model calls, one per goal1
run; goal2 and goal3 raised no UNKNOWN. The scorer agrees with the runtime on 8 of the 9 tasks.

| Goal | C-LLM #1 | C-LLM #2 | C-LLM #3 |
|---|---|---|---|
| goal1 SFP, dropout | **retry, success, 75** | **retry, success, 75** | **retry, success, 75** |
| goal2 SFP, dropout | FORCE_LIMIT twice, failed, 0 | success, 75 | success, but the scorer: **partial, 50** |
| goal3 SC, no fault | success, 75 | success, 75 | success, 75 |

- **Every call answered.** All three came back as a `retry` tool call (`finish_reason:
  tool_calls`) after 9.4, 10.0 and 8.1 s of wall time, with 745, 881 and 648 completion tokens
  for a 1176-token prompt. All three were under 1024, so they fit the old budget as well: this
  run shows the calls came back, not that the larger budget was needed. In
  the F1 rerun 5 of 6 calls stopped at 1024; the evidence for the budget is the mock comparison
  (2 of 15 answers cut at 1024, none at 4096).
- **Each retry stepped down.** In every goal1 run the dropout began during align (12.0 s), the
  Monitor called UNKNOWN (`insert_stalled`) 1.05 s into `insert_scripted`, and the link came
  back at 27.0-27.1 s, while the model was thinking (2.25-2.75 s of sim time passed during the
  call). The retried `insert_scripted` started after that and seated the plug in 1.65 s: from
  12.2-12.6 mm down to its seat (-0.97 to -0.98 mm), at most 0.54 mm between two 50 ms samples
  (a scripted step is 0.5 mm), at most 5.8 N in our samples. The scorer saw no force over 20 N
  and gave 75 each. The timing is #61's goal1 #1, the link back before the retry starts, where
  the retry jumped 12 mm in 0.15 s; the F1 fix is what changed. As in #61, the link was back
  when the retry started, which the model cannot know.
- **goal2 #1 started in the wrong place.** From its first sample (0.10 s) the plug was at the
  port's height (-1.3 mm) and 31 mm off its axis, and the cameras show the gripper beside the
  task board; in #2 and #3 it started 137 mm above the port, 40 mm off the axis. Before trial 2
  the engine's log (`logs61/rllm1.sim.log`) reads "Successfully reset joints to home position,
  robot homed successfully.", as in run 2; why this one started there is not established. Being
  under 20 mm already, the plug set off the dropout at 0.15 s, during approach. The link came
  back at 15.2 s, and at 26.5 s the force reached 27.7 N; that align went on from where its
  targets had got to (a case the F1 fix does not cover) is the likely reading, not one the data
  shows. The Monitor called
  FORCE_LIMIT, B's table retracted slowly (a known class, so no model call), and the second
  align met 20.4 N at 47.6 s, with the plug 20.5 mm off the port axis and the FORCE_LIMIT budget
  used up. The scorer: task not completed (0), force over 20 N for 0.63 s with a peak of
  28.05 N, under its 1 s, no penalty.
- **goal2 #3 is the first disagreement with the scorer.** The dropout began at 13.9 s in align
  (16.8 mm), and the plug went on down, as in #2, but settled near 0 mm (lowest -0.24 mm)
  instead of the SFP seat at -0.98 mm. `insert_scripted` found it seated at once and `verify`
  passed, because the runtime's seated band is -3 to +1 mm, one band for both port types
  (`adapters/aic/skills.py`, set from #28's SFP seats at -0.98 mm and an SC seat at +0.07 mm).
  The scorer called it a partial insertion (50, "distance of -0.00m"). The band is too wide for
  an SFP plug; a band per port type is a change to the skills, left for a later PR. (Since
  then each port type has its own band: SFP -1.5 to -0.6 mm, SC -0.5 to +0.5 mm, #70.)
- **goal2 #2:** the dropout began at 14.9 s in align (15.7 mm) and the plug went on down to its
  seat (-0.97 mm), as in #61's goal2 under A: a fault with no effect.
- **goal3 (SC, no fault):** seated during align in all three runs (75 each). In the F1 rerun it
  jammed at the entrance in two of three. Across the 13 goal3 runs on record, the plug's
  distance from the port axis as it reached the entrance sorts the outcomes. Here it was
  0.10-0.18 mm in each run; every plug 0.5 mm or more off stuck on the rim. That reading led to
  align's hold above the entrance (ADR 024).

## Media

Recorded in the VM from the three wrist cameras (headless Gazebo) and composed with the event
log as a caption panel, at 2x speed. Kept outside the repository until the project site is
reviewed:

- `c_llm_goal1_retry_hero.mp4` (12 s, 292 KB): the stall, the model thinking while the arm
  holds, the retry's jump seating the plug.
- `c_llm_goal1_full.mp4`, `a_goal1_full.mp4` (the same fault under A), `c_llm_goal2_escalate.mp4`.
- `trace_c_llm_goal1_wall.json`, `trace_c_llm_goal1_sim.json`: `cellrt trace` of goal1 C-LLM #1;
  on the wall clock the planner call fills 10.65 s of the run.
- `c_llm_goal2_retry_hero.mp4` (12 s, 324 KB), `c_llm_goal2_retry_full.mp4` (48 s, 1.3 MB) and
  a poster frame: the rerun's goal2 C-LLM #2, whose retry steps the plug down to its seat. The
  rerun's other two C-LLM runs were recorded too; their frames are on the VM's disk, not
  rendered.
- From the 4096-token rerun: `c_llm_goal1_retry_4096_hero.mp4` (13 s, 413 KB) and a poster
  frame, goal1 C-LLM #1: the stall, 9.4 s of the model thinking, the retry stepping down. All
  three runs were recorded.
- `demo.mp4` (55 s, 1.2 MB, 2x): four title cards and three of these clips in order: #61's
  retry that jumped, the F1 rerun's retry that stepped down, and this rerun's.

## Reproduce

The runbook's sections 4-6 and its section 10, with the policy settings from environment
variables (README, "The AIC cell"): `CELLRT_SETTING`, `CELLRT_PLANNER`, `CELLRT_FAULT`, and for
`llm` the key in `NVIDIA_API_KEY`. Session names were `s61clean` (clean), `s61` (A, B, C-rule) and
`s61llm` (C-LLM, attempts a1 and a2); in the rerun, `sf1clean` and `sf1llm` (attempts a1 to a3)
in their own store; in the 4096-token rerun, `srllm` (attempts a1 to a3, with
`CELLRT_MAX_TOKENS=4096` and `CELLRT_REASONING=default`) in a third. Inspect a run from a copy
of the store:

```bash
cellrt replay s61llm/goal1/task_1/C/0/a1 --db sim61-copy.sqlite
cellrt trace s61llm/goal1/task_1/C/0/a1 --db sim61-copy.sqlite --out trace.json
cellrt replay sf1llm/goal2/task_1/C/0/a2 --db simf1-copy.sqlite
cellrt replay srllm/goal2/task_1/C/0/a3 --db simr-copy.sqlite
```
