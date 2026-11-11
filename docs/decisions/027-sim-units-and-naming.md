# 027: Sim units: scenes named by task id, the seed as the repeat, one trial per unit

Date: 2026-11-11
Status: accepted

**This decision changes what eval results mean.** It changes how a sim run is named, and so
which runs count as attempts of one unit and which as separate units. It amends ADR 010,
question 6.

## Context

ADR 010 fixes the unit of work: one (scenario, setting, seed). A unit is complete when one of its
attempts has `run_finished` and that attempt's `config_hash` matches the current configuration;
an interrupted unit reruns as a new attempt.

In the cell, a unit is `<session>/goal<N>/<task id>` today (ADR 010, question 6, Option A;
`adapters/aic/run.py:204-210`):
- the engine sends no trial id;
- the policy counts its goals, and `CELLRT_SESSION` names the engine config;
- the seed is always 0 (`run.py:269-279`).

ADR 010 names the limits of goal counting:
- a trial the engine skips shifts every later goal;
- two engine configs under one session share ids.

Its Option D, a runner that writes the engine configs and names each goal's scenario, was left
for Phase 2.

Phase 2 needs three things the counting cannot give:
- **Repeats of the same scene.** The pilot runs A twice per scene, and the held-out eval runs
  each scene three times per setting (ADRs 026, 030). With the seed fixed at 0, a planned repeat
  and a rerun after an interruption both become attempt `a2` of one unit, and ADR 010 counts
  only the first finished attempt.
- **A scene identity that survives reordering.** The batch runner rebuilds each engine config
  from the units still unfinished (ADR 028), so goal N names a different scene in every engine
  run.
- **A run that says which scene it ran.** Otherwise the report must rebuild the mapping from
  the engine configs and the order of the goals.

What reaches the policy is the `Task`, built from the trial's `tasks:` block; its `id` is that
block's key (`aic_engine.cpp:224-248`; `task_1` in every sample trial). The engine only prints
the id.

## Options

### 1. How a goal names its scene

#### Option A: keep counting goals (ADR 010, question 6, Option A)
- Pros: nothing to build.
- Cons: the limits above; a rebuilt engine config renames every goal.

#### Option B: the task id names the unit, and a manifest that the policy reads maps it to the scene
- Pros: the policy is told what it runs, by the only field the engine passes through; a
  reordered or rebuilt engine config keeps every name; an unknown id can be refused before the
  arm moves.
- Cons: one more file the policy must read and trust; a hand-written engine config without a
  manifest still needs Option A.

#### Option C: the runner writes the next goal's scene to a side file before each goal
- Pros: no manifest.
- Cons: the runner does not know when the engine sends the next goal, so the file can be stale.

### 2. What the seed means in the sim

#### Option A: always 0 (today)
- Pros: nothing to change.
- Cons: a planned repeat and an interrupted rerun look the same.

#### Option B: the repeat index
- Pros: each planned repeat is its own unit; an interruption stays an attempt of its unit.
- Cons: in the sim the seed draws no randomness, so "seed" means only "repeat k". The mock's
  "same seed, same log" does not hold: Gazebo is not bit-for-bit repeatable.

#### Option C: a seed for randomness in the sim
- Pros: the word would keep its mock meaning.
- Cons: nothing in the sim path takes a seed we control.

### 3. What the scenario part of the configuration holds

#### Option A: the task only (today)
- Pros: nothing to change.
- Cons: two scenes with the same task would share a configuration, so a finished run of one
  would count for the other.

#### Option B: the task plus a digest of the scene's rendered trial
- Pros: any change to the scene, or to how it is rendered (the template, the generator), is a
  new configuration, and finished units of the old one are not reused.
- Cons: the parameters live in the manifest, not in the event log; the log holds only the
  digest.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option B for each question.

**Naming.**
- Every task id in an engine config we write is unique across all manifests: `<scene id>_r<k>`,
  where k is the repeat. The id matches `^[a-z0-9_]{1,64}$`.
- A manifest (JSON, one per split: `pilot-v1`, `tune-v1`, `heldout-v1`) holds:
  - the manifest's name;
  - each scene's parameters and digest;
  - each task id's scene and repeat.
- `adapters/aic/manifest.py` loads it with a strict model and a size bound: at most 1 MiB, and
  at most 2,000 task ids. `lookup(task_id)` returns the scenario id `<manifest>/<scene id>`, the
  seed k, and the scene digest.
- `RuntimePolicy` reads the path from `CELLRT_MANIFEST` once, on its first goal, as it reads the
  other settings.
  - A goal whose task id is not in the manifest is refused before any command is sent: the
    policy returns False and logs a fixed code. The batch runner sees no finished run and
    records the code in its ledger.
  - A manifest that fails to load refuses every goal in the same way.
- Without `CELLRT_MANIFEST`, nothing changes: ADR 010's goal counting stays for engine runs
  started by hand.

**Run ids.** `<manifest>/<scene id>/<setting>/<k>/a<attempt>`, the mock's
`scenario/setting/seed/aN` with the scenario named by the manifest.

**Seed.** `run_goal(..., seed=k, scene_digest=...)` records k as the run's seed. Nothing in the
AIC path draws from it; in the sim it only names the repeat.

**Scenario part.** It holds the task and the scene digest: the SHA-256 of the scene's rendered
trial node (sorted keys, compact JSON). This is the same way an injected fault joins the
scenario part today (`run.py:395-407`).

**Unit.** One trial: one (scenario, setting, seed), with ADR 010's completion rule unchanged in
the core. The batch runner adds one condition for the sim: the trial's referee data must also
have been collected (ADR 028).

## Why

- **Name what you run, at the source.** The task id is the one field the engine carries from
  our config to the policy, so naming the unit there removes the counting and every failure
  that counting brings. The manifest is the contract between the generator and the policy. It
  is versioned, hashed and committed before any run (ADR 030), so a run id can always be traced
  back to the exact scene.
- **The seed as the repeat** keeps ADR 010's unit and its resume logic intact. Repeats are
  distinct units, and interruptions are attempts. The weakness to name first: "seed" no longer
  means a random seed in the sim. The results document says so, because a reader of the mock's
  "same seed, same result" would expect it.
- **The digest of the rendered trial, not of the parameters**, because the template and the
  renderer can change what Gazebo spawns without any parameter changing.

## Consequences

- ADR 010, question 6, is amended: in engine runs started by the batch runner, Option D holds.
- A hand-started engine run on the sample config works as before.
- The report joins runs to scenes through the manifest, never through goal order.
- The policy now reads one more file. Its size bound and fixed refusal codes are tested
  (#78).
- To revisit: if the engine ever sends a trial id, the manifest could key on it instead.
