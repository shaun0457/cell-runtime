# 015: Ground truth: what it is for, and who may read it

Date: 2026-11-09
Status: accepted; the counting on sim runs amended by 029 (each verdict judged against the insertion time)

**This decision changes what eval results mean** (it fixes how verdicts on runs without an
injected fault are counted, and states that every sim result so far depends on perfect
geometry).

## Context

In the sim, `ground_truth:=true` publishes the exact poses of the port, the plug and the gripper
as TF frames. The official eval runs with `ground_truth:=false`, so these frames are never
published there, and its access list also denies the model the scoring and simulator topics
(`docker/aic_eval/aic_zenoh_config.json5:293-321` in the AIC repo). A real cell has nothing
like them. The project uses ground truth (GT) in four
places today:

1. **Referee labels.**
   - Mock: the injected fault's label (`fault_injected`), which the metrics use to judge Monitor
     verdicts.
   - Sim: the scorer's insertion result. #28 found it in the trial bag
     (`/scoring/insertion_event`), because the scorer's YAML hides it when the policy returns
     False.
2. **The skills' geometry.**
   - `gripper_target()` reads the port, plug and gripper poses every step
     (`adapters/aic/geometry.py`).
   - Ground truth stands in for perception, which is out of scope.
3. **The Monitor's inputs in the sim.**
   - `AicWorld` fills `Signals.height_mm` and `xy_offset_mm` from the GT plug and port frames
     (`adapters/aic/world.py`, `plug_to_port_mm` in `adapters/aic/convert.py`).
   - The progress, stuck, stall and pose rules read them (`cellrt/monitor/classifier.py`).
   - Only force and torque are measured.
4. **The runtime's own success check.** `verify` compares the GT plug height with a threshold
   (`adapters/aic/skills.py`).

The project never wrote down which of these uses are legitimate. Two things made that urgent:
- #28 showed that the runtime's judgement in the sim rests on GT. Two seated plugs were judged
  failed because of a GT-height rule and a placeholder threshold.
- Shaun asked whether to drop ground truth and let an LLM judge instead.

The mock world is not affected in the same way. Its XY measurement is truth plus noise, and its
signals are its own.

How the metrics count verdicts today (`cellrt/eval/metrics.py`):
- a verdict with no injected fault active is a false alarm;
- a fault with no verdict is a miss.

In the sim, a clean run can fail for real; #28's SC plug jammed at the entrance. A scene fault
can also be compensated by the skills before anything fails. Both cases would be counted wrong.

## Options

### What ground truth may be used for

#### Option A: remove it; an LLM or VLM judges success and geometry
- Pros: closer to a real cell; uses the LLM.
- Cons:
  - Nothing independent is left to measure the system against: it would grade itself.
  - An LLM takes seconds and cannot sit in the 50 ms Monitor loop.
  - A model cannot give millimetre poses, so the skills would need a perception stack, which
    is out of scope.

#### Option B: keep using it everywhere, as today, without saying so
- Pros: nothing to change.
- Cons: every sim number is silently conditional on perfect geometry. "The runtime judges
  success by itself" is not true and would not survive an interviewer tracing
  `Signals.height_mm` back to TF.

#### Option C: three roles with explicit permissions
- Pros:
  - Keeps an independent referee.
  - Says plainly what the runtime may read.
  - Leaves a place for a model's judgement as a measured prediction.
- Cons:
  - Admits that the runtime in the sim is not GT-free today.
  - Needs care in every new feature.

#### Option D: a proprioception-only runtime now (TCP pose, wrench, port pose from calibration)
- Pros: closest to a real cell without perception.
- Cons:
  - The TCP-to-plug offset still depends on the grasp, and the port pose on a calibration that
    a scene fault can invalidate.
  - Weeks of work.
  - The #28 review advises it only as a time-boxed experiment.

### How a verdict counts on a run without an injected fault (sim only)

#### Option A: as a false alarm, always (the current metric)
- Pros: one rule for the mock and the sim.
- Cons: a real jam on a clean run counts as a false alarm, and a compensated scene fault counts
  as a miss.

#### Option B: by the referee's result
- Pros: separates the Monitor's mistakes from real failures the faults did not cause.
- Cons: needs the scorer's insertion result per trial; a second rule beside the mock's.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option C for the roles, and Option B for counting.

| Data | May the runtime use it online? | What a result means |
|---|---|---|
| Referee labels: scorer insertion events and results, injected fault labels | **Never.** Only the offline eval reads them | The independent answer |
| Oracle geometry: GT poses as a stand-in for perception | Yes, in an oracle mode that is named as such | Results are conditional on perfect geometry |
| Measured observations: wrench, controller TCP pose, joint states, camera images | Yes | Results depend on the stated calibration and estimators |
| A model's judgement (LLM or VLM) | Only as a slow, bounded, optional opinion that may abstain | A prediction, scored against the referee; never the success criterion |

- **Every AIC run so far is in oracle mode.** The skills, the Monitor's height and XY signals,
  and `verify` all read GT geometry. Every AIC run already records this:
  `run_started.geometry_source` is `"ground_truth"` (event schema version 4; the mock world
  leaves it empty). A second geometry source adds a value to that field.
- **Counting on sim runs without an injected fault:**
  - a verdict on a trial the scorer saw inserted is a **false alarm**;
  - a verdict on a trial the scorer did not see inserted is a **natural failure**, counted in
    its own row;
  - an injected scene fault with no verdict on a trial the scorer saw inserted has **no
    effect**. It is reported as compensated, not as a miss.
- **The mock keeps its current rule.** It has no scorer. Its numbers and the committed
  baseline do not change.
- The sim counting is built with the Phase 2 runner, which collects the insertion events.

## Why

- **A system cannot grade itself.** The runtime must judge its own success to act, and it does.
  But the evidence that it judged right has to come from something it cannot read. In the sim
  that is the scorer. Dropping ground truth in favour of an LLM would delete the only way to
  tell whether the LLM was right.
- **Each piece has its place:**
  - perception is out of scope, so oracle geometry is the honest stand-in;
  - the Monitor is fast and deterministic;
  - a model is slow and fallible, so it can only be a second opinion, measured like any other
    predictor.
- **The weakness to name first:** the runtime is not GT-free in the sim. Its Monitor and its
  success check read perfect geometry. A real cell needs perception or a fixture calibration in
  that place. The design keeps that seam (`Signals`), so the core would not change.

## Consequences

- Claims about sim results carry "with oracle geometry". The README and the interview material
  say so.
- #28's report and the Phase 2 reconciliation use the insertion events as the referee, not the
  scorer's YAML.
- A VLM check of "is the plug seated" is allowed as an experiment. It must be scored against the
  insertion events, with false successes, false failures and abstentions reported. It is not
  planned: the roadmap keeps perception and VLMs out of scope (`docs/roadmap.md`).
- A proprioception-only check is a later, time-boxed experiment. If it works, it becomes a
  second geometry source with its own label.
- To revisit: once a perception or calibration source exists, which rules may read it, and
  whether the oracle mode stays as a ceiling to compare against.
