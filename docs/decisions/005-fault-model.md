# 005: How faults are injected and how they vary by seed

Date: 2026-11-08
Status: accepted

## Context

Settings A, B and C are compared on the same scenarios. For the comparison to mean anything:
every failure class has to be producible on demand; classification accuracy needs a ground-truth
label the Monitor cannot see; and results must vary across seeds (a fault that is identical on
every seed gives each (scenario, setting) either 0 % or 100 %, which makes pass^k and recovery
rate meaningless). The injected ground truth must not leak into what skills or the Monitor read.

Current shape: a scenario lists fault specs (a kind plus parameter ranges). For each
(scenario, seed), `materialize` draws concrete parameters from `random.Random(
"{scenario_id}:{seed}:faults")`, a stream separate from the world's noise
(`"{scenario_id}:{seed}"`); the setting is never part of either key. The mock world applies an
active fault as an effect and reports activation and clearing to the runner, which records `fault_injected` / `fault_cleared` with the label.
Skills receive a `WorldView` that exposes only `signals()` and `command()`.

## Options

### Where a fault acts

#### Option A — in the world (the runtime has to detect it from signals)
- Pros: tests the Monitor for real; mirrors Phase 2, where faults are perturbations of the sim
  (board pose, stalled controller, sensor topic dropout).
- Cons: every fault needs a world-level story; some failures (a skill bug) cannot be expressed.

#### Option B — in the skill (a skill is told to fail with a class)
- Pros: trivial to implement; every class is exact.
- Cons: classification accuracy becomes meaningless (the class is given, not detected); the
  Monitor is never exercised.

### How a fault varies

#### Option A — seeded parameter ranges per fault (onset, magnitude, persistence)
Persistence is expressed per kind: a duration for timed faults (`controller_stall`,
`sensor_dropout`); faults triggered by the plug's state (`pose_shift`, `obstacle`, `tight_fit`,
`yaw_mismatch`) take effect when the plug reaches them. Obstacles and pose shifts persist for a
seeded number of attempts (insertions into the port, descents past the trigger height); see
ADR 007 for why retries need it. A yaw mismatch persists until the plug is twisted to within
1° of the port (seeded misalignment 5–15°); only `insert_act` twists, following the torque's
sign, so B cannot clear it; setting C is meant to clear it by switching to `insert_act`
(ADR 008). Twisting 30° takes as long as the 1 s stall window, so larger misalignments read as
a stall even for `insert_act`; the seeded range stays well below that.
- Pros: the same seed reproduces exactly; across seeds a bounded retry budget sometimes suffices
  and sometimes does not, so B and C show partial recovery.
- Cons: ranges need tuning to get a readable spread; "persistence" means different things for
  different kinds.

#### Option B — fixed faults, only nominal noise varies with the seed
- Pros: easier to reason about a single run.
- Cons: results per (scenario, setting) are close to binary.

### RNG streams

#### Option A — separate streams for faults and world noise, both keyed by (scenario, seed)
- Pros: adding or changing a fault does not perturb the world's noise; setting never enters the
  key, so A/B/C face identical conditions.
- Cons: two seeds to explain instead of one.

#### Option B — one stream for everything
- Pros: simplest.
- Cons: the draw order couples faults and noise; editing a fault silently changes every
  measurement in that scenario.

## Decision

<!-- Decision and Why drafted by Claude from the option the code took. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

- Faults act in the world, not in skills (Option A): the Monitor has to detect them from
  signals.
- Each fault kind has seeded parameter ranges for onset, magnitude and persistence (Option A).
- Fault parameters and world noise come from two RNG streams, both keyed by (scenario, seed);
  the setting is never part of a key (Option A).
- Skills only see a `WorldView` (`signals()`, `command()`), so the injected label cannot leak.

## Why

Classification accuracy only means something if the Monitor has to find the fault in the
signals; a skill told to fail with a class would make accuracy 100 % by definition. Seeded
ranges make pass^k and recovery rate informative: with fixed faults, every (scenario, setting)
is either 0 % or 100 %.

Separate streams keep experiments clean: editing one fault does not reshuffle the noise in every
measurement. Keeping the setting out of the key is what makes A, B and C face identical
problems, and the fairness tests check it from the log.

The honest weakness: the ranges were chosen by us, so B's success rate per scenario was chosen
too. The claim is the gap between settings under identical faults, and Phase 2 tests it against
faults the mock did not design.

## Consequences

- Every Phase 0 failure class can be produced on demand and is labelled, so classification
  accuracy and a confusion matrix come straight from the log. Because each fault was designed to
  match one Monitor rule, the 100 % accuracy of the Phase 0 eval shows that rules and attribution
  work, not that the Monitor would classify real failures this well (`docs/phase0-results.md`).
- Who wins is set by our ranges: fault persistence against the recovery budgets (ADR 007) decides
  B's success in `obstacle`, `pose_shift` and `controller_stall`; the dropout range (0.5–1.0 s)
  is inside the 2 s sensor wait, and half speed keeps every drawn `tight_fit` under 20 N.
- The baseline succeeds in every seed (300 of 300, no false alarm), not the ~95 % the Phase 0
  plan expected: nominal noise is far below the thresholds, and the mock was not changed to
  produce failures. The nominal failure rate comes from the sim in Phase 2.
- Parameter changes after the first full eval (`3b4a4eb`, #9): none so far. Any later change to
  `MockParams` or to the fault ranges in `eval/scenarios/phase0.yaml` is listed here with its
  reason.
