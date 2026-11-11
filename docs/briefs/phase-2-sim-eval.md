# Phase 2 brief: what recovery is worth, measured in the sim

Input for `to-spec`. It states the problem, the scope, the facts and the exit criteria; the
open decisions are listed with options for plan mode and ADRs. Issue #13 holds the detailed
open questions; this brief turns them into a phase. Facts are checked against `shaun0457/aic`
@ `3ab0653`.

**Revised after #28 (2026-11-09).** The first sim run (`docs/phase1-run28-report.md`) and a
review of the plan (`docs/reviews/2026-11-09-plan-grill.md`) cut this phase down:
- a cost-capped **fault discovery** pilot comes first, and the eval is small;
- the scene channel comes first; another channel is added only if the pilot finds nothing to
  recover;
- ACT is optional;
- the large noise-floor study is replaced by a few clean repeats.

Phase 3 now starts on the mock world before this phase.

## Problem

Phase 0 measured recovery on a mock world whose parameters we chose, so its A/B/C gaps are
partly our own design (`docs/phase0-results.md`). #28 ran the runtime in the AIC cell end to
end with no faults. The pipeline, the stop path and the scorer comparison work there. But the
runtime judged two seated plugs as failures, and the SC plug jammed at the port entrance in
two of three runs.

Nothing yet says what recovery is worth when the physics, the sensor noise and the faults are
not ours. That is the claim the project exists to make. It has to be made in a way that
survives an interviewer who asks "did you tune it until B won?"

## Goal

First, find out what the sim gives us to recover from:
- a cost-capped pilot perturbs the scene;
- each outcome is sorted as compensated by the skills, known failure, UNKNOWN, or adapter
  abort.

Then run a small A/B/C eval in Gazebo, with a resumable runner, on the faults that produce
something. It reports success, recovery, unsafe force and Monitor accuracy with their
uncertainty, reconciled trial by trial with the AIC scorer, and it states plainly where B or C
gains nothing.

If the pilot finds no UNKNOWN, that is reported as a result. A known class is never relabelled
UNKNOWN to make one.

## Prerequisites

1. Phase 1's gaps from #28 are closed, or stated as limits:
   - seated detection;
   - the align/insert boundary;
   - the SC entrance jam;
   - the unconfirmed hold after a timeout.

   The runbook works from a fresh VM. The run report gives the real-time factor (0.43) and the
   first F/T numbers.
2. The ADRs Phase 2 builds on are accepted: 005 (fault model), 006 (Monitor), 007 (recovery
   budgets), 009 (metrics), 010 (resume and units), 011–014 (time, stop path, force sampling,
   command mode). #43 accepts 001–014; Decision and Why are drafted by Claude under Shaun's
   delegation. Phase 2's own decisions that change what the results mean (fault channels, the
   held-out split, metric changes) are flagged in their PRs for Shaun before merge.
3. The ground-truth roles are decided (ADR 015): scorer labels are the referee, and the
   adapter's geometry is an oracle stand-in for perception.

## In scope

- A **fault discovery pilot** with a stated cost ceiling, run before the eval.
- An **eval runner** that:
  - writes one engine config per unit;
  - starts the engine run;
  - names each goal's scenario for the policy (ADR 010, question 6, option D);
  - collects our store, the scorer's YAML and the scorer's insertion events from the trial bag;
  - resumes after preemption.
- **Fault injection** through the channels below, each recorded with a ground-truth label. The
  scene channel comes first.
- **Monitor calibration** from measured sim data, with a tuning split and a held-out split.
- **Recovery in the cell**: setting B and C trees with the scripted skills; stop confirmation
  between skills (deferred from Phase 1, ADR 012); which recovery is valid from which state.
- **Reconciliation** of our metrics with the AIC score per trial. The YAML cannot tell an
  insertion apart when the policy returns False, so the insertion events are needed.
- A results document in the style of `docs/phase0-results.md`.
- **Optional, only with time left:** ACT as `insert_act`, wrapped as a skill. It needs
  `get_observation`, camera images and velocity mode.

## Out of scope

- The LLM planner (Phase 3; it is developed on the mock world in parallel). Setting C keeps
  `RulePlanner` in this phase's eval unless Phase 3's path is ready.
- New policies or training. ACT is used as it is; a poor `insert_act` is a finding, not a bug.
- Perception. Ground-truth TF stays the geometry source; perception error is modelled (channel 2).

## Facts the design must respect

| Fact | Where |
|---|---|
| Each trial's scene is configurable: task-board pose, which entity sits on each rail, its translation along the rail, and its roll, pitch and yaw | `aic_engine/config/sample_config.yaml:56-128` |
| `task_board_limits` clamps rail translation only; entity rotations pass through unclamped | `sample_config.yaml:46-55`; `aic_engine.cpp:1798-1830` |
| The cable's grasp is configurable per trial: `gripper_offset` x, y, z and roll, pitch, yaw, and the cable type (`sfp_sc_cable` or `sfp_sc_cable_reversed`) | `sample_config.yaml:129-140` |
| A rail can be left empty (`entity_present: False`) | `sample_config.yaml:75-82` |
| Each task has its own `time_limit` (180 s in the sample) | `sample_config.yaml:141-150` |
| A trial can contain several tasks; one `InsertCable` goal per task; no trial id reaches the policy | `aic_engine.cpp:218-237`, `:1353`; ADR 010 §6 |
| Scores are written as YAML per trial and tier to `$AIC_RESULTS_DIR` (default `~/aic_results`) | `aic_engine.cpp:266-273`, `:321-329`, `:661` |
| Off-limit contacts come from `/aic/gazebo/contacts/off_limit`; the scorer only records them, it does not stop the arm | `sample_config.yaml:22-23`; `ScoringTier2.cc:273`, `:413-415` |
| ACT: velocity mode, ~4 Hz, a 26-dim state and camera images, `shaun0457/act_aic`; heavy imports must be lazy | CLAUDE.md, AIC facts; `RunACT.py` |
| Phase 1 subscribes to the F/T wrench topic (50 Hz) and does not call `get_observation` | #19, ADR 013 |
| Gazebo physics is not bit-for-bit reproducible across runs; the mock's "same seed, same log" does not carry over | to measure (decision 2) |
| When `insert_cable` returns False, the scorer marks the task not completed and scores tiers 2 and 3 as 0, even with the plug in; the trial bag's `/scoring/insertion_event` still records the insertion | #28 (`docs/phase1-run28-report.md`) |
| The skills read ground-truth geometry every step, so a scene fault (yaw, grasp offset) may be compensated before anything fails; an empty rail may end the run as `ground_truth_unavailable` before any verdict | `adapters/aic/geometry.py`, `adapters/aic/world.py` |
| Real-time factor on an L4 is about 0.43; one engine run of 3 trials took 3.5–8 min of wall time | #28 |

## Design decisions (plan mode; each non-trivial one becomes an ADR)

1. **Fault channels and what each may touch.** Phase 0's rule still holds: a fault acts on the
   world, and the Monitor must detect it from signals, never from the label (ADR 005).
   Candidate channels:
   1. *Scene*, through the engine config: entity rotation (a yaw mismatch the scripted skill
      may not handle), translation, grasp offset and rotation, the reversed cable, an empty rail.
      Real physics, but coarse and fixed for a whole trial.
   2. *Perception error*: bias or noise on the ground-truth geometry the **skills** read, never
      on the Monitor's inputs. Models the error a real perception stack would have; without it,
      oracle geometry may make most faults trivially compensated.
   3. *Sensing*: dropped or delayed F/T samples in the adapter's buffer (SENSOR_DROPOUT).
   4. *Actuation*: commands withheld for a window (controller stall, TIMEOUT).
   For each: the label, how the actual applied value is recorded (read back the spawned pose,
   not the requested one), and which failure class it is expected to produce. A scene fault the
   skill compensates is a valid result: the scenario then measures nothing and says so.
   **Revised:** the discovery pilot uses the scene channel only. Each perturbation is sorted as
   compensated, known failure, UNKNOWN or adapter abort. Channel 2 (perception error) is added
   only if the pilot leaves nothing to recover. Channels 3 and 4 stay in the mock world. Scene
   faults are chosen by us: the honest claim is that the failure trajectories come from the
   physics, not that the failures were never designed.
2. **Fairness without determinism.** A, B and C face the same engine config and the same fault
   parameters, but the physics differs run to run. **Revised:** no large noise-floor study. What
   stays:
   - a few clean repeats per setting (k stated before running);
   - success with intervals (Wilson or bootstrap) instead of bare percentages;
   - settings interleaved in the queue, so a preemption or a slow VM hour does not bias one
     setting.

   The fairness tests from Phase 0 still check that the faults were identical; they can no
   longer check that the runs were.
3. **The runner and the unit.** Option D of ADR 010 §6: the runner writes the engine config,
   so it names the scenario and the policy no longer counts goals. Queue order, how a preempted
   engine run is detected and restarted, what is rerun (the unit, or the whole engine run), and
   the opt-in VM shutdown when the queue is empty.
4. **Calibration and the held-out split.** Thresholds come from measured F/T rate, noise and
   tare residual (the Phase 1 run report). Scenarios used to tune Monitor rules and recovery
   parameters are separate from the scenarios that produce the reported numbers. Include
   permanent obstacles and unrecoverable faults (an empty rail, a wrong cable) so B and C can
   show no gain. Decide FORCE_LIMIT debounce with real noise (ADR 006 has none).
5. **ACT as a skill (optional).** How the ~4 Hz wall-time cadence of ACT fits a loop that steps on sim time
   (ADR 011), how images reach it (`get_observation`), what its postconditions are, and how a
   switch from a scripted insert to ACT hands over the arm (stop confirmation first).
6. **Recovery validity per state and connector.** Depth, orientation and withdrawal direction per
   plug and port type (SFP, SC); which recovery may start from which state; a restart from a state
   the skill was not validated for is rejected, not attempted.
7. **Off-limit contact.** Wrist F/T cannot see the arm touching the enclosure. Prevent it with
   motion and workspace limits; take the ground truth offline from the contact topic (about 5 Hz,
   not an interlock) and count it in the unsafe metric beside force.
8. **Reconciliation with the scorer.** Per trial:
   - our runtime-verified success against the scorer's insertion result, taken from the
     insertion events because the YAML hides it when the policy returns False;
   - our time over 20 N against the scorer's force penalty;
   - our off-limit count against its contacts.

   Every disagreement is listed with its cause. How a Monitor verdict counts on a run without
   an injected fault follows ADR 015: a false alarm if the scorer saw the plug inserted,
   otherwise a natural failure counted on its own.
9. **Budget.** Sim time per task is about 32 s, plus spawn and scoring overhead, divided by the
   real-time factor measured in #28 (0.43). Choose scenarios × settings × repeats from a stated
   cost ceiling, before running, and write the arithmetic down. The discovery pilot has its
   own, smaller ceiling.

## Exit criteria

1. A discovery report: every perturbation tried, its outcome class (compensated, known
   failure, UNKNOWN, adapter abort) and the cost of the pilot.
2. The runner runs a queue of units on a Spot VM, survives a forced preemption (kill the VM
   mid-queue, restart), and the final table equals the table of an uninterrupted run up to sim
   noise, with every unit's attempts in the log.
3. Clean repeats per setting, with the spread of success and time over 20 N.
4. The A/B/C table from held-out scenarios, with intervals, plus a per-scenario breakdown
   with each scenario's fault channel and expected class.
5. The Monitor's confusion matrix in the sim, with false alarms and natural failures on clean
   runs counted as ADR 015 defines them.
6. A reconciliation table against the AIC scorer for every trial, with each disagreement
   explained.
7. A results document that states:
   - which scenarios show no gain for B or C;
   - what `insert_act` achieved, if it was built;
   - what the mock got wrong.

## Suggested first slice (tracer bullet)

The runner writes one engine config for the clean baseline, runs setting A once on the VM,
collects our store, the scorer YAML and the insertion events, and prints one reconciliation row.
It touches the runner, the engine config, the scenario naming, the store and the scorer parser
before any fault exists. Then the discovery pilot on the scene channel, then the clean repeats,
then the eval.

## Risks

- Most faults may be compensated by oracle geometry, leaving B and C nothing to recover. The
  perception-error channel exists for this; if gains still vanish, that is the finding.
- Sim cost grows fast with repeats; without the budget arithmetic first, the eval stops halfway.
- Tuning on the scenarios that produce the numbers, even unintentionally. The split is fixed
  before calibration starts and recorded in the results document.
- ACT's real performance is unknown on this pipeline (it never fully inserted in the challenge);
  setting C's one rule switches to it, so C may be worse than B. Report it either way.
- The pilot may find no UNKNOWN at all. Then Phase 3's sim case is a negative result, and its
  mock path stands on its own.
