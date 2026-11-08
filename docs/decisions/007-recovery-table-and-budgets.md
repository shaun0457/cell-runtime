# 007: How setting B recovers known failure classes

Date: 2026-11-08
Status: proposed

## Context

Setting B must show what deterministic recovery is worth over setting A: for the same scenario
and seed, B should recover runs that A loses, but only for failures the Monitor classifies as
known (ADR 006), and never loop forever. Every recovery has to be visible in the event log so the
eval can count recoveries per class and so a run can be explained after the fact.

Current shape: the B tree is `Sequence[Recovering(step) for step in approach, align,
insert_scripted, verify]`. A `Recovering` composite runs its step's skill; when a skill inside it
fails with a class that has a plan and the budgets allow it, it records `recovery_started`
(skill, class, action, per-class count, total count), runs the plan's skills, then the failed
skill again, and repeats. Otherwise it fails with a reason that names the exhausted budget.
Budgets are shared across the whole task. All parameters, the plan table included, live in
`RecoveryConfig` (validated, immutable), which is part of `config_hash` for settings that
recover. Helper limits are checked against the helpers' own timeouts so a helper never times
out first and spends the TIMEOUT budget.

| Class | Plan | Budget |
|---|---|---|
| POSE_ERROR | align (rises to hover first), then the skill again | 2 |
| INSERT_STUCK | retract to 5 mm above the port, align, then the skill again | 2 |
| FORCE_LIMIT | retract, then the skill again at half speed | 1 |
| TIMEOUT | the skill again | 1 |
| SENSOR_DROPOUT | wait for 3 valid samples (at most 2 s), then the skill again | 1 |
| UNKNOWN | none: the run fails (setting C hands it to the planner) | — |
| any class | — | 4 per task |

To make retries meaningful, faults triggered by the plug's state persist for a seeded number of
attempts: an obstacle blocks the first N insertions (an insertion is the plug tip going from
above the port to inside it), a pose shift moves the port on the first N descents past its
trigger height (N drawn from 1–3 in `phase0.yaml`). A tight fit is recovered by speed, not by
persistence, and a yaw mismatch is not recovered in B at all.

## Options

### Where the recovery logic lives

#### Option A — one custom composite per task step (`Recovering`)
- Pros: the recovery table reads as data (`PLANS`); one place handles budgets, events and the
  failure reason; the plan skills are real tree children, so `bt_node_changed` shows them.
- Cons: a custom composite is less familiar than stock py_trees nodes; its tick loop has to
  follow py_trees' contract (initialise, stop children, yield itself). A plan with no helper
  skills (TIMEOUT) reruns the skill in the same tick, so `bt_node_changed` shows no FAILURE for
  it: failures are read from `monitor_verdict` and `recovery_started`, not from node statuses.

#### Option B — stock py_trees nodes (Selector, Retry, blackboard conditions per class)
- Pros: standard BT vocabulary, visualisable with py_trees tools.
- Cons: "retry until success or budget" needs a Retry decorator that spends extra ticks
  after the last failure; per-class branches and budget checks multiply nodes per step; a
  half-speed rerun needs its own branch anyway.

### What a plan runs again

#### Option A — the failed skill, then the rest of the step
- Pros: a dropout in align is retried in align, a timeout in approach in approach; one rule for
  every class and step.
- Cons: "insert at half speed" needs a separate slow node for the step's skill, and only the
  insert skills read `speed_scale`; a FORCE_LIMIT in another step (not reachable in the mock)
  would rerun at full speed while the log records the scale.

#### Option B — always the whole step from its first skill
- Pros: simplest.
- Cons: wasteful and wrong for prefix skills (a failed align inside a POSE plan would restart
  the insert first).

### Budgets

#### Option A — per class plus a task total, shared across steps
- Pros: bounds the run even when classes alternate; matches the kickoff table; counts are
  comparable across runs.
- Cons: a budget spent early (a dropout in approach) is gone for a later step.

#### Option B — per step
- Pros: each step gets a fair chance.
- Cons: total recoveries per task grow with the number of steps; harder to reason about.

### What "retract" means

#### Option A — back out to a fixed height above the port (5 mm, the hover height)
- Pros: the next insertion is a new attempt in the world (it re-enters the port), so a fault
  that persists for N attempts can actually clear; independent of how deep the plug was.
- Cons: differs from the literal "retract 5 mm" in the kickoff table.

#### Option B — move up a fixed distance
- Pros: literal reading.
- Cons: from deeper than 5 mm the plug stays inside the port, so "re-align" cannot move it and
  the retry is not a new attempt.

### Fault persistence across retries

#### Option A — a seeded number of affected attempts per fault
- Pros: across seeds, budgets sometimes suffice and sometimes not, so recovery rate is partial
  and pass^k means something.
- Cons: "attempt" is defined from world state (entries into the port, descents past a height),
  which is a modelling choice the AIC sim does not make for us.

#### Option B — faults are permanent (or one-shot)
- Pros: simpler.
- Cons: B either never or always recovers a given fault: results per scenario become binary.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.
