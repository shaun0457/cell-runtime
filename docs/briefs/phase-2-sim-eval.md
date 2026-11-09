# Phase 2 brief: what recovery is worth, measured in the sim

Input for `to-spec`. It states the problem, the scope, the facts and the exit criteria; the
open decisions are listed with options for plan mode and ADRs. Issue #13 holds the detailed
open questions; this brief turns them into a phase. Facts are checked against `shaun0457/aic`
@ `3ab0653`.

## Problem

Phase 0 measured recovery on a mock world whose parameters we chose, so its A/B/C gaps are
partly our own design (`docs/phase0-results.md`). Phase 1 proved the runtime drives the AIC
cell with no faults. Nothing yet says what recovery is worth when the physics, the sensor noise
and the faults are not ours. That is the claim the project exists to make, and it has to be
made in a way that survives an interviewer who asks "did you tune it until B won?"

## Goal

An A/B/C eval in Gazebo with injected faults, run by a resumable runner on the Spot VM, that
reports success, recovery, unsafe force and Monitor accuracy with their uncertainty, reconciled
trial by trial with the AIC scorer, and that states plainly where B or C gains nothing.

## Prerequisites

1. Phase 1 done (#28): the 3 sample tasks pass, the runbook works from a fresh VM, and the run
   report gives the real-time factor and the first F/T noise numbers.
2. **Shaun confirms the Decision and Why of the ADRs Phase 2 builds on**: 005 (fault model),
   006 (Monitor), 007 (recovery budgets), 009 (metrics), 010 (resume and units), 011–014 (time,
   stop path, force sampling, command mode). The Phase 1 spec (#19) records its choices as
   "Claude defaults"; Phase 2 decides what the results mean, so it cannot rest on unconfirmed
   decisions. #17 holds drafts for 001–005 and 008–010.

## In scope

- An **eval runner** that writes one engine config per unit, starts the engine run, names each
  goal's scenario for the policy (ADR 010, question 6, option D), collects our store and the
  scorer's YAML, and resumes after preemption.
- **Fault injection** through the channels below, each recorded with a ground-truth label.
- **Monitor calibration** from measured sim data, with a tuning split and a held-out split.
- **Recovery in the cell**: setting B and C trees with the scripted skills; stop confirmation
  between skills (deferred from Phase 1, ADR 012); which recovery is valid from which state.
- **ACT as `insert_act`**, wrapped as a skill (needs `get_observation`, camera images and
  velocity mode).
- **Reconciliation** of our metrics with the AIC score per trial.
- A results document in the style of `docs/phase0-results.md`.

## Out of scope

- The LLM planner (Phase 3). Setting C keeps `RulePlanner`.
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
2. **Fairness without determinism.** A, B and C face the same engine config and the same fault
   parameters, but the physics differs run to run. Options: measure a noise floor first (the
   baseline unit rerun k times per setting), report success with intervals (Wilson or
   bootstrap) instead of bare percentages, interleave settings in the queue so a preemption or a
   slow VM hour does not bias one setting. The fairness tests from Phase 0 still check that the
   faults were identical; they can no longer check that the runs were.
3. **The runner and the unit.** Option D of ADR 010 §6: the runner writes the engine config,
   so it names the scenario and the policy no longer counts goals. Queue order, how a preempted
   engine run is detected and restarted, what is rerun (the unit, or the whole engine run), and
   the opt-in VM shutdown when the queue is empty.
4. **Calibration and the held-out split.** Thresholds come from measured F/T rate, noise and
   tare residual (the Phase 1 run report). Scenarios used to tune Monitor rules and recovery
   parameters are separate from the scenarios that produce the reported numbers. Include
   permanent obstacles and unrecoverable faults (an empty rail, a wrong cable) so B and C can
   show no gain. Decide FORCE_LIMIT debounce with real noise (ADR 006 has none).
5. **ACT as a skill.** How the ~4 Hz wall-time cadence of ACT fits a loop that steps on sim time
   (ADR 011), how images reach it (`get_observation`), what its postconditions are, and how a
   switch from a scripted insert to ACT hands over the arm (stop confirmation first).
6. **Recovery validity per state and connector.** Depth, orientation and withdrawal direction per
   plug and port type (SFP, SC); which recovery may start from which state; a restart from a state
   the skill was not validated for is rejected, not attempted.
7. **Off-limit contact.** Wrist F/T cannot see the arm touching the enclosure. Prevent it with
   motion and workspace limits; take the ground truth offline from the contact topic (about 5 Hz,
   not an interlock) and count it in the unsafe metric beside force.
8. **Reconciliation with the scorer.** Per trial: our runtime-verified success against the
   scorer's insertion result, our time over 20 N against the scorer's force penalty, our
   off-limit count against its contacts. Every disagreement is listed with its cause.
9. **Budget.** Sim time per task is about 32 s, plus spawn and scoring overhead, divided by the
   real-time factor measured in #28. Choose scenarios × settings × repeats from a stated cost
   ceiling, before running, and write the arithmetic down.

## Exit criteria

1. The runner runs a queue of units on the Spot VM, survives a forced preemption (kill the VM
   mid-queue, restart), and the final table equals the table of an uninterrupted run up to sim
   noise, with every unit's attempts in the log.
2. A noise-floor report: the baseline rerun k times per setting, with the spread of success and
   time over 20 N.
3. The A/B/C table from held-out scenarios, with intervals, plus a per-scenario breakdown
   with each scenario's fault channel and expected class.
4. The Monitor's confusion matrix in the sim, including false alarms on clean runs.
5. A reconciliation table against the AIC scorer for every trial, with each disagreement
   explained.
6. A results document that states which scenarios show no gain for B or C, what `insert_act`
   achieved, and what the mock got wrong.

## Suggested first slice (tracer bullet)

The runner writes one engine config for the clean baseline, runs setting A once on the VM,
collects our store and the scorer YAML, and prints one reconciliation row. It touches the
runner, the engine config, the scenario naming, the store and the scorer parser before any
fault exists. Then the noise floor, then one fault channel end to end, then the rest.

## Risks

- Most faults may be compensated by oracle geometry, leaving B and C nothing to recover. The
  perception-error channel exists for this; if gains still vanish, that is the finding.
- Sim cost grows fast with repeats; without the budget arithmetic first, the eval stops halfway.
- Tuning on the scenarios that produce the numbers, even unintentionally. The split is fixed
  before calibration starts and recorded in the results document.
- ACT's real performance is unknown on this pipeline (it never fully inserted in the challenge);
  setting C's one rule switches to it, so C may be worse than B. Report it either way.
