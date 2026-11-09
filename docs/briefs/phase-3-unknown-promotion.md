# Phase 3 brief: the LLM planner and UNKNOWN promotion

Input for `to-spec`, written early so Phase 1 and 2 do not close doors Phase 3 needs. Open
questions on timing, what the planner may decide and what it is given are in #14; this brief
adds the goal of the phase and one design that ties it to the rest of the project.

**Revised after #28 (2026-11-09).** The first sim run and a review of the plan
(`docs/reviews/2026-11-09-plan-grill.md`) changed three things:
- Phase 3 starts on the mock world before Phase 2, so it no longer waits for a Phase 2 store.
- Its first exit criterion is one complete LLM path with baselines and a trace. Task-to-tree,
  promotion and a VLM experiment come after it.
- The model is reached through an OpenAI-compatible API (NVIDIA NIM).

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

They are not delivered together. **First comes one complete path, triage with escalation:**

1. a Monitor UNKNOWN verdict;
2. the arm in HOLD;
3. an asynchronous model call with a wall-time deadline and a call cap;
4. schema and state validation;
5. a bounded action (`SwitchSkill`, `Retry`, `Abort`) or `Escalate`. `Escalate` is new: it
   changes the decision schema (`cellrt/planner/schemas.py`) and ADR 008;
6. the run's outcome.

The path is measured against simple policies with the same budget. Task-to-tree and promotion
follow only once this path stands.

**Why the decision must matter.** The path is only worth showing if the right answer depends
on the evidence. Phase 0's one UNKNOWN (`yaw_mismatch`) has a single right answer, switching to
`insert_act`, which `RulePlanner` hard-codes. The Phase 3 scenarios therefore add an
unrecoverable UNKNOWN with a different signature, where the right answer is to stop early.
Both scenarios are designed by us, and the results say so.

## The model provider

- The provider is NVIDIA NIM's hosted API (`https://integrate.api.nvidia.com/v1`).
- It is OpenAI-compatible, so the client is the `openai` SDK, installed as an optional extra.
  Shaun chose this provider and approved the dependency on 2026-11-09. An ADR records the
  choice when the code lands.
- The core defines its own small client protocol and never imports the SDK; tests use a fake
  client with scripted answers.
- The key is read from `NVIDIA_API_KEY` and never written to the log.
- Which hosted model supports tool calling is checked with one smoke call before building on
  it. If tool calling is unreliable, the model answers in JSON instead; that is validated the
  same way.

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
   Replay checks how the **classification rule** behaves on recorded inputs. It cannot show
   that the proposed **recovery** works: that needs new runs. The candidate is also checked on
   held-out logs that played no part in the proposal.
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
  #28 saw one hold that was not confirmed (33 mm/s after an align timeout), so the check is
  real, not a formality. In the mock world HOLD stops the mock arm at once, so the check passes
  trivially there. Once the new path exists, the mock's sim clock must not advance while a call
  is pending: the arm holds and the wait costs wall time only.
- **The executor cannot wait on outside work yet.** `drain()` expects every live task to wait
  on the sim clock and raises after 100 yields (`cellrt/executor/runner.py`). A real model call
  needs its own path: a wall-time deadline, cancellation when the tree is stopped, and a late
  answer discarded as an event.
- **No fork yet.** `state_at()` rebuilds the state at any step, the basis for a fork; forking a
  run (in the mock or in Gazebo) is not built, and nothing here may claim it.
- **Replay must stay exact.** Record the planner's prompt and raw response in the event log
  (`planner_called`, `planner_decided`), so `cellrt replay` never calls a model. An eval with a
  real model is a sample, not a deterministic run: report its variance across repeats, as Phase 2
  does for sim noise.
- **Promotion can only use what was recorded.** A candidate rule can read only the fields in
  `signal_sampled` (per step: tared force and torque, depth, XY error, wrench validity,
  `force_over_limit_s`). A rule that needs a new signal needs new runs before it can be
  validated; old stores cannot check it.
- **Where the model runs.** Calls go to an API from wherever the run happens: the laptop for
  the mock path, the VM for sim runs. The key comes from the environment (a secret store on
  the VM), never from the repo or the engine config. An opt-in CI job, if one is added, uses a
  protected secret with a call cap.

## Out of scope

- An LLM in the control loop, or emitting velocities, gains or overrides of safety verdicts
  (#14).
- Adopting a rule without human approval.
- Training a classifier: rules stay explainable thresholds.

## Later options

- **A VLM as a second opinion on "is the plug seated"** (candidate: NVIDIA Cosmos Reason 2;
  check its current API before building on it), measured
  against the scorer's insertion event:
  - it reports false successes, false failures and abstentions, not only accuracy;
  - it also takes images where runs ended early or failed, not only where they reached `verify`;
  - first check whether the insertion event means "was ever inserted" or "is seated now".
- **An agent framework** (LangGraph, NeMo Agent Toolkit) is not adopted. An ADR states what
  this cell needs and what a framework would cost to maintain, without claiming that event
  sourcing makes frameworks worse.

## Exit criteria (draft)

First, the one complete path:

1. Triage with escalation runs end to end in the mock world through the validation gate.
   Invalid output is an event, never a crash: tests with a fake client, and one opt-in run with
   a real model and a call cap.
2. Every planner call has a wall-time deadline and a per-run call cap; a late answer is
   discarded, and the call is cancelled when the tree stops it.
3. The log holds a full trace of each call: the rendered prompt, the raw response, the model
   id, token counts and latency. Replay never calls a model, and a trace export shows run,
   skill and planner spans.
4. An eval against `RulePlanner` and a fixed retry/abort policy with the same budget reports
   success, wasted steps, planner calls, invalid-output rate, timeouts, latency (p50/p95) and
   tokens. A real model is repeated at least 3 times for variance. A result with no gain is
   reported as one.

Then, if the path stands:

5. Task to tree passes the same gate.
6. A promotion run produces at least one candidate rule. Recurring UNKNOWN groups come from
   Phase 2 data or from extra mock scenarios with different signatures; promotion waits for
   one of them. The rule comes with a replay report:
   - it fires on its group;
   - it raises zero false alarms on baseline runs;
   - other verdicts are unchanged;
   - it holds on held-out logs.
7. The eval reports UNKNOWN recurrence before and after promotion.
