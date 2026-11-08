# 006: The Monitor decides failure classes and preempts the running skill

Date: 2026-11-08
Status: proposed

## Context

Settings B and C recover by failure class (ADR 007 will hold the recovery table), and the eval
reports classification accuracy against the injected ground truth. So one component has to turn
what the runtime can observe into a `FailureClass`, early enough to stop a skill that is making
things worse (a plug pushed into an obstacle keeps building force), and without ever reading the
fault label.

Current shape: `Monitor.observe(signals)` is called on every tick of the running skill (in
`SkillBehaviour.update`, after the done check and before the timeout check). A verdict cancels
the skill and records `monitor_verdict` (class, rule, evidence) followed by
`skill_cancelled(reason="preempted")`. The signal window holds only the current skill's samples
(`reset` on skill start) and is bounded by the longest rule (22 samples at 20 Hz). Timeouts are
classified by the same Monitor (`classify_timeout`). Each scenario injects one fault; how the
world combines several insertion faults (an obstacle inside a tight section) is not modelled.
Rules, checked in priority order:

| Class | Rule | Applies to |
|---|---|---|
| SENSOR_DROPOUT | wrench invalid for 0.2 s | every skill |
| FORCE_LIMIT | force > 20 N for more than 1.0 s | every skill |
| INSERT_STUCK | force > 10 N, < 0.2 mm descent over 0.5 s, XY within 1.0 mm | insert skills |
| POSE_ERROR | XY error > 1.0 mm for 0.2 s | insert skills |
| UNKNOWN | < 0.2 mm descent over 1.0 s, no rule above matched | insert skills |

"Held for D" means the samples from the first to the last span at least D (more than D for
FORCE_LIMIT), rounded up to whole periods, so the verdict lands D after onset and never earlier;
evidence records the span actually covered. Progress is measured as descent of the plug height, not as depth, so the approach
from hover height to the port surface counts as progress.

## Options

### Who decides the failure class

#### Option A — the Monitor only; a skill reports ok / not ok
- Pros: one place to read and test the rules; classification accuracy measures one component;
  skills stay simple and replaceable (the ACT skill cannot classify its own failures anyway).
- Cons: the Monitor needs to know which skills insert (a name list today); a skill's own
  knowledge of why it failed is not used.

#### Option B — skills return a class; the Monitor only adds timeouts
- Pros: no extra component; the skill knows its intent.
- Cons: every skill re-implements the same thresholds; learned skills cannot do it; accuracy is
  spread across skills.

### When the class is decided

#### Option A — continuously, preempting the skill
- Pros: stops harm while it happens (force keeps rising against an obstacle); mirrors the AIC
  penalty, which is about duration over the force threshold.
- Cons: false alarms cancel healthy skills, so the baseline must be checked for zero verdicts;
  rules need windows to ignore single noisy samples.

#### Option B — only when the skill ends (or times out)
- Pros: simpler; no cancellation path.
- Cons: a skill that never ends on its own runs to its timeout under force; every failure would
  look like TIMEOUT.

### Window scope

#### Option A — per skill (reset on start)
- Pros: insertion rules never see the previous skill's samples (a hover before insert would
  read as a stall); a recovery skill starts with a clean slate.
- Cons: a condition that spans a skill boundary restarts its count (a dropout must last long
  enough inside one skill; the scenario uses ≥ 0.5 s so either side holds 0.2 s).

#### Option B — one window for the whole run
- Pros: conditions spanning skills are seen in full.
- Cons: phase-specific rules need their own bookkeeping; a recovery skill can be preempted at
  once by samples from before it started.

### Rules that overlap

#### Option A — fixed priority, first match wins
- Pros: deterministic, one verdict per tick, easy to explain.
- Cons: the order is a judgement call (safety first: no valid force reading, then the AIC force
  penalty, then mechanical causes).

#### Option B — score every rule and report the strongest
- Pros: could express confidence.
- Cons: scores need calibration the mock world cannot justify.

### What UNKNOWN means

#### Option A — "stalled, and no known rule explains it"
- Pros: gives setting C a real job; a new failure mode shows up as UNKNOWN rather than as a
  wrong known class.
- Cons: the yaw-mismatch fault is shaped so no known rule fires (force 6–9 N, below the 10 N
  stuck threshold; the kickoff sketch said 8–12 N, which would overlap INSERT_STUCK); its high
  torque is recorded in the evidence but no rule reads torque.
- Cons: "stalled" is any insert that does not descend 0.2 mm in 1 s, so a stalled controller
  during insertion (label TIMEOUT) would read as UNKNOWN, and so would an insert skill that
  moves up for more than 1 s. Today the stall scenario's onset always lands in approach and
  `insert_scripted` only moves down; both change once retries and the ACT skill arrive.

#### Option B — a dedicated torque rule (a known YAW class)
- Pros: more accurate for this fault.
- Cons: leaves nothing for the planner in Phase 0.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.
