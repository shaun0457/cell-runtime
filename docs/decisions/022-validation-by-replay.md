# 022: Checking a candidate Monitor rule by replay

Date: 2026-11-10
Status: proposed

## Context

A promoted rule (ADR 021) starts as a model's proposal. Principle 4 says model output is
validated before it runs; for a Monitor rule, validating the schema is not enough. The question
is whether the rule describes the failures it claims to describe, and nothing else.

The runtime records every sample the Monitor judged (`signal_sampled`, ADR 004) and the mock is
deterministic, so the Monitor can be run again over a stored run. Two facts shape how:

- The Monitor judges a sample on the tick after it is logged, for the skill running then; a
  skill's first tick only starts it. A run stopped by the time limit ends without a tick after
  its last sample, which was therefore never judged; a run stopped because the world stopped
  judged its last sample before the step that failed.
- After the first verdict of a skill attempt, the recorded trajectory is the one the old
  handling produced (a retract, a planner call). What a candidate would have changed after that
  point is not in the log.

Usual practice: detection rules in security operations are backtested on stored logs before
they are enabled, and trading or fraud rules are backtested on history. Both rely on the replay
being faithful first; a backtest that cannot reproduce what the live system did says nothing.

## Options

### 1. What replay runs

#### Option A — the real `Monitor` over the stored samples, in the order the run judged them
Reset on `skill_started` with the skill's profile and, in a live planner wait only, on
`planner_called` with the hold profile (a frozen wait judges nothing until the next skill
starts); the force time comes from the fold (`apply`), as in the run.
- Pros: one implementation of every rule; a replay that reproduces the record is evidence that
  the inputs are complete.
- Cons: replay depends on the executor's event order, which a change to the executor can break
  (the reproduction gate then fails loudly).

#### Option B — a separate offline evaluator of rule conditions
- Pros: simpler, independent of the executor.
- Cons: two implementations of the rule semantics, which can disagree without anyone noticing.

### 2. Which configuration replay uses

#### Option A — the caller names it; replay refuses unless its hashes match the run's
The `world`, `monitor` and `skills` parts of `config_parts` must equal the hashes of the
configuration given (period, thresholds, skill profiles).
- Pros: the run only stores hashes; this checks without storing values.
- Cons: a store made with a non-default configuration needs that configuration passed in.

#### Option B — store the values in `run_started`
- Pros: replay needs no input.
- Cons: a schema change, and old stores still have only hashes.

### 3. The gates

- **Gate 0, reproduction:** without the candidate, every recorded verdict is reproduced (class,
  rule, evidence, on the same sample); a verdict that lost to a skill's success and a skill
  timeout are accounted for. Otherwise the check is refused.
- **Gate 1, coverage:** with the candidate, it is the first verdict of every skill attempt in its
  group, no later than the recorded UNKNOWN.
- **Gate 2, no false alarms:** it is never a first verdict in a run without an injected fault.
- **Gate 3, no theft:** every other attempt's first verdict is unchanged.

#### Option A — all four, each a hard gate
- Pros: each failure mode of a proposed rule has its own check and its own failing test.
- Cons: a rule that covers most but not all of a group is rejected outright.

#### Option B — coverage as a rate with a threshold
- Pros: tolerates a noisy group.
- Cons: another threshold to choose; the mock is deterministic, so a miss is a real miss.

### 4. What replay does not show

Replay compares verdicts. It cannot show whether the plan works, because the recorded runs never
ran it. That needs a new eval run with the rule adopted (the before/after in the spec). The
report says so.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

1. Option A: the real Monitor over the stored samples, in the run's judging order.
2. Option A: the caller names the configuration; replay refuses on a hash mismatch.
3. Option A: four hard gates, gate 0 first.
4. The report states that the plan is checked only by a new eval run.

## Why

The point of the check is that a model's rule is never trusted on its own account, so the check
itself must be trustworthy: one Monitor implementation, and a first gate that proves the replay
reproduces the live verdicts before any candidate is judged. Comparing only each attempt's first
verdict keeps the check honest about the counterfactual: the log says what the signals were up
to the moment the old handling took over, and nothing after it. Hard gates fit a deterministic
mock; a threshold would be tuning without data.

## Consequences

- A change to the executor's event order shows up as a failed gate 0, not as wrong results.
- Mock stores replay with the default configuration; an AIC store needs the adapter's period and
  skill profiles passed in (not built yet).
- Stores made before `xy_offset_mm` was recorded cannot be replayed; their runs are counted as
  skipped.
