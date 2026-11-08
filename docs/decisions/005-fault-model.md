# 005: How faults are injected and how they vary by seed

Date: 2026-11-08
Status: proposed

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
`yaw_mismatch`) take effect when the plug reaches them and, for now, stay in effect. A number
of affected attempts comes with the recovery ticket, where retries make it matter.
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

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.
