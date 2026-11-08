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
`SkillBehaviour.update`, before the done check and before the timeout check). A verdict cancels
the skill and records `monitor_verdict` (class, rule, evidence, and `also_matched` when other
rules held on the same sample) followed by `skill_cancelled(reason="preempted")`; on the tick a
skill completes, a safety verdict (SENSOR_DROPOUT, FORCE_LIMIT) overrides a success and any
verdict overrides the skill's own failure (`monitor_verdict`, then the skill's own
`skill_finished`; a metric that counts `skill_finished.ok` must pair it with the verdict). Two bounded windows: the safety
window (sensor validity, force) runs across skills and restarts after a safety verdict; the
insertion window covers the current skill only (`reset` on skill start). Timeouts are
classified by the same Monitor (`classify_timeout`). `MonitorConfig` is passed to `run_unit` and
is part of `config_hash`. A class a skill returns itself is used only without a verdict and is
marked `source="skill"` on `recovery_started`; a skill that raises aborts the run. Each scenario injects one fault; how the
world combines several insertion faults (an obstacle inside a tight section) is not modelled.
Rules, checked in priority order:

| Class | Rule | Applies to |
|---|---|---|
| SENSOR_DROPOUT | wrench invalid for 0.2 s | every skill but `wait_sensor` |
| FORCE_LIMIT | force > 20 N for more than 1.0 s | every skill |
| INSERT_STUCK | < 0.2 mm descent over the last 0.5 s, with force > 10 N and XY within 1.0 mm at the current sample | insert skills |
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

#### Option C — the Monitor first; a class from the skill only when there is no verdict
- Pros: keeps the skill contract's optional `failure` field; conflicts have a rule; the source
  is recorded, so accuracy can be reported per source.
- Cons: two sources of classes to explain; a skill can still mislabel its own failure.

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

### Failures the Monitor did not classify

Preconditions not met, a skill returning `ok=False` without a class, and a skill raising.

#### Option A — each gets its own reason; a raise aborts the run
- Pros: a bug is never "recovered"; the planner (ADR 008) only ever sees Monitor UNKNOWN.
- Cons: more outcomes to report.

#### Option B — all of them are UNKNOWN
- Pros: one path to the planner.
- Cons: hides bugs as failures; muddies classification accuracy.

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

#### Option C — safety durations across skills, insertion progress per skill
- Pros: force and sensor time is counted the way the AIC penalty counts it, whatever skill is
  running; insertion rules still never see the previous skill's samples. Restarting the safety
  window after a safety verdict keeps the skill that handles it from being preempted at once.
- Cons: two windows to reason about; a force episode that a verdict ended is not counted again
  until it has lasted the full duration a second time. Only a safety verdict restarts the
  window: after a normal completion or another class's verdict, the next skill inherits the
  time already counted. A dropout that began in approach can preempt align early, and a
  retract that follows INSERT_STUCK under high force can be preempted by FORCE_LIMIT before it
  has unloaded (rare in the mock, where retract unloads fast).

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

<!-- DRAFT by Claude from the 2026-11-08 grilling session. Shaun rewrites Decision and Why in
his own words, then sets Status to accepted. -->

- **Authority.** The Monitor decides failure classes. A skill reports ok / not ok. If a skill
  also returns a class, it is used only when the Monitor gave no verdict, and the event records
  that the class came from the skill, so classification accuracy is reported per source.
- **Timing.** The Monitor checks every tick of the running skill and preempts it on a verdict.
  On the tick a skill completes, the Monitor is still asked first: a safety verdict
  (SENSOR_DROPOUT, FORCE_LIMIT) overrides the skill's "done", and any verdict overrides the
  skill's own report of a failure.
- **Windows.** Force-over-limit and sensor-invalid durations are tracked across skill
  boundaries, the way the AIC force penalty counts them. The progress window that the
  insertion rules use restarts with each skill.
- **Overlapping rules.** Fixed priority, first match wins: can we see (SENSOR_DROPOUT), is it
  safe (FORCE_LIMIT), what is the mechanical cause (INSERT_STUCK, POSE_ERROR), else UNKNOWN.
  The evidence also lists the other rules that held on the same tick.
- **UNKNOWN** means "stalled during insertion and no known rule explains it". It is not a
  detector of new failure modes. In Phase 0, `yaw_mismatch` stands in for the failures a real
  cell meets that no rule covers.
- **Failures without a verdict** get their own reasons, not UNKNOWN: preconditions not met, the
  skill reported failure, the skill raised an exception. Only a Monitor UNKNOWN goes to the
  planner (ADR 008); an exception aborts the run.
- **Hand-over after preemption.** One skill owns the command channel at a time. On preemption
  or timeout the executor sends HOLD before the next skill starts. HOLD stops adding motion; it
  does not unload force. Unloading is a recovery action (retract, ADR 007).
- **Which skills insert** is declared by each skill (`monitor_profile`: `insertion`,
  `sensor_wait`, ...), not by a name list inside the Monitor.

## Why

<!-- DRAFT by Claude: Shaun rewrites this in his own words, as he would explain it in an
interview. -->

One component owns the question "why did this fail", for three reasons:

1. **Consistency.** One set of thresholds, and the same Monitor in settings A, B and C, so the
   settings differ only in what they do after a failure.
2. **Replaceable skills.** The Phase 2 insert skill is a learned ACT policy: it outputs
   velocities and cannot say why it is stuck. Classification cannot live in the skills.
3. **Conflicts have a rule.** Monitor first, the skill's class only as a labelled fallback.

Because of this, classification accuracy can be measured as one component against the
injected ground truth, separately from whether recovery works.

Preempting instead of waiting for the skill: a plug pushed into an obstacle keeps building
force. Waiting for the 15 s timeout means paying the AIC force penalty and labelling every
failure TIMEOUT, so the BT could not pick a fitting recovery. The cost is that a false alarm
cancels a healthy skill, which is why the baseline must produce zero verdicts.

The priority order follows from what each rule needs: force rules are meaningless without a
valid force reading, and safety comes before diagnosis.

UNKNOWN is shaped on purpose (yaw mismatch stays below the stuck threshold and no rule reads
torque). The challenge "you designed a fault your rules cannot see" is fair: the answer is that
a real cell always meets failures the rules do not cover, and Phase 0 needs one to show what
the planner adds over B. Phase 2 tests this against failures we did not design.

## Consequences

- Easier: thresholds are tuned and tested in one place; per-class accuracy and a confusion
  matrix come straight from `monitor_verdict` against `fault_injected`.
- Harder: the Monitor needs to know what each skill is doing (`monitor_profile`); every rule
  change needs the baseline zero-verdict check.
- Thresholds come from the AIC scoring rule and the mock world, not from a safety analysis.
  The controller's own protective stop stays the hard safety layer below the Monitor.
- Out of scope for Phase 0: graded responses (slow down before stopping), compliant
  (impedance/admittance) control, estimating the contact phase from signals instead of from
  the skill. Revisit the last one when the ACT skill spans several phases.
- Known misreads: a stalled controller during insertion reads as UNKNOWN, not TIMEOUT; an
  insert skill moving up for more than 1 s reads as stalled; several insertion faults at once
  are not modelled.
- Implementation status: everything above is in place except the HOLD hand-over and
  `monitor_profile`, which land with #8. The 20-seed A/B sweep gave the same success counts
  before and after the cross-skill windows and the Monitor-before-done change.
