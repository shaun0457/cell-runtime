# Phase 3 brief: the LLM planner and UNKNOWN promotion

Input for `to-spec`, written early so Phase 1 and 2 do not close doors Phase 3 needs. Open
questions on timing, what the planner may decide and what it is given are in #14; this brief
adds the goal of the phase and one design that ties it to the rest of the project.

## Problem

In Phase 0 the planner is a rule stub, and the C − B gap (14 points) is exactly the
`yaw_mismatch` scenario: it shows that the validation gate and the budgets work, not that a
planner adds value. `yaw_mismatch` also exposes a design smell: it has a clear signature (a
stall at a fixed depth with high torque) and is UNKNOWN only because no rule reads torque. A
real cell should not keep sending the same unknown failure to an expensive planner; it should
learn a rule for it.

## Goal

Three LLM roles, each behind the validation gate Phase 0 already built (principle 4):

1. **Triage** of Monitor UNKNOWN verdicts: replaces `RulePlanner` behind the same protocol.
2. **Task to tree**: from an AIC `Task` (plug and port type, target), produce a behaviour tree
   that uses only registered skills; validated against a schema and the skill registry before
   it runs. This is LLM-driven program generation with a verifier in front of execution.
3. **Escalation**: hand over to a human through an approval step when no allowed option fits.

## The design that ties it together: UNKNOWN promotion

When UNKNOWN verdicts with the same signature recur, the LLM proposes a new Monitor rule or a
recovery plan from the recorded evidence. The proposal is adopted only after it passes an
offline check against the event log:

1. **Group.** Recurring UNKNOWN verdicts are grouped by their evidence (the Monitor already
   records force, torque, progress and XY error in `monitor_verdict`).
2. **Propose.** The LLM receives a group's evidence and the rule language (thresholds over
   `Signals` fields and windows, as in ADR 006) and returns a candidate rule plus a recovery
   plan built from registered skills. Output is schema-validated like any planner output.
3. **Validate by replay.** The candidate is run over stored `signal_sampled` streams:
   - it must fire on the runs in its group (the failures it claims to explain);
   - it must not fire on any baseline run (no false alarms);
   - it must not change verdicts on runs the existing rules already classify, unless the
     proposal says so explicitly.
   This works only because Phase 0 records every signal sample and is deterministic (ADR 004).
4. **Adopt.** A rule that passes becomes a new class in the Monitor and a row in the recovery
   table, behind a human approval and a new `config_hash`. Results before and after are never
   mixed (ADR 010).

What this shows: the planner's long-term value is turning unknown failures into known,
deterministic handling, and the system can check an LLM's proposal before trusting it. The
metric for it: how often an UNKNOWN with the same signature reaches the planner, before and
after promotion.

## What Phase 1 and 2 must keep possible

- Keep recording every signal sample in the sim (or every sample the Monitor sees), so replay
  can validate a candidate rule. If Phase 2 downsamples storage, the Monitor's inputs must
  still be reconstructible.
- Keep Monitor rules data-driven enough that a new rule is configuration plus a small,
  reviewed code change, not a rewrite.
- Keep `monitor_verdict` evidence rich (force, torque, progress, XY, the rules that also
  matched).

## Constraints learned in Phase 1 and 2

- **Sim time keeps running while the LLM thinks.** A planner call takes wall time; at a
  real-time factor below 1 it costs less sim time, at 1 the same. Either way the arm must be in
  a confirmed HOLD before the call (ADR 012 confirms the stop only at run end today; Phase 2 adds
  it between skills), and the wait comes out of `Task.time_limit` (180 s in the sample config).
- **Replay must stay exact.** Record the planner's prompt and raw response in the event log
  (`planner_called`, `planner_decided`), so `cellrt replay` never calls a model. An eval with a
  real model is a sample, not a deterministic run: report its variance across repeats, as Phase 2
  does for sim noise.
- **Promotion can only use what was recorded.** A candidate rule can read only the fields in
  `signal_sampled` (per step: tared force and torque, depth, XY error, wrench validity,
  `force_over_limit_s`). A rule that needs a new signal needs new runs before it can be
  validated; old stores cannot check it.
- **Where the model runs.** Calls go from the Spot VM to an API: the key lives in a secret
  store, never in the repo or the engine config, and the opt-in CI job uses a protected secret
  with a call cap.

## Out of scope

- An LLM in the control loop, or emitting velocities, gains or overrides of safety verdicts
  (#14).
- Adopting a rule without human approval.
- Training a classifier: rules stay explainable thresholds.

## Exit criteria (draft)

1. Triage, task-to-tree and escalation each run through the validation gate; invalid output is
   an event, never a crash (tests with a fake LLM; one opt-in job with a real model and a call
   cap).
2. Every planner call has a wall-time timeout; a late answer is discarded.
3. A promotion run on the Phase 2 store produces at least one candidate rule, with a replay
   report (fires on its group, zero baseline false alarms, other verdicts unchanged).
4. The eval reports planner calls per task and UNKNOWN recurrence before and after promotion.
