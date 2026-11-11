# 030: Splits, calibration scope and the held-out protocol

Date: 2026-11-11
Status: accepted

**This decision changes what eval results mean.** It fixes, before any data exists, which scenes
may be used to tune, which ones produce the reported numbers, what may be tuned, and when a gain
may be claimed (pre-registration, part 2; part 1 is ADR 026).

## Context

The claim has to survive "did you tune it until B won?". Three facts make that a real risk:
- **The scene list is ours** (ADR 026). Choosing scenes after seeing results would let the
  list decide the answer.
- **The Monitor thresholds can be tuned.** The AIC skills carry their own Monitor profiles and
  recovery config. Tuning them on the same scenes that produce the numbers would fit the
  numbers.
- **The sample is small.** About 150 held-out trials fit the budget. Gazebo is not repeatable,
  so every rate needs an interval. With a 95% Wilson interval near p = 0.5, the half-width is
  about 0.11 at n = 72 and 0.25 at n = 12.

Shaun decided on 2026-11-11:
- **D4.** All three manifests are generated now, from disjoint seeds, and committed with their
  hashes before any VM run. The checkpoint picks families by rule, never single scenes.
- **D5.** Calibration is measured first. A change is allowed only to the AIC Monitor profiles
  or the AIC `RecoveryConfig`, only when tuning trials show that a threshold is the cause, and
  only with an ADR and an unchanged mock baseline.
- **D6.** The eval runs A and B. C-LLM is added only where the pilot finds an UNKNOWN, with at
  most 50 model calls per session.
- **D7.** If the pilot finds nothing, that is reported, and the eval falls back to the natural
  SC jams, the tilt families and the controls.

Time: the interview is around 2026-11-16, and the freeze point is 2026-11-15 morning.

## Options

### 1. When the splits are fixed

#### Option A: all three now, before any run (D4)
- Pros: no scene can be chosen after its result is known.
- Cons: if the pilot shows a family is broken (for example, parts interpenetrate), its tuning
  and held-out scenes are generated anyway and must be dropped by rule.

#### Option B: tuning and held-out generated after the pilot
- Pros: they can use what the pilot taught.
- Cons: every choice made after seeing data is a fork the result can hide in.

### 2. What the checkpoint may choose

#### Option A: families, by a rule written now
- Pros: the choice is mechanical; anyone can redo it from the pilot table.
- Cons: may keep a family that turns out to be dull.

#### Option B: free choice of families or scenes
- Pros: flexible.
- Cons: exactly the freedom the claim must not have.

### 3. The interval and the claim

#### Option A: Wilson 95% per setting; a gain only when A's and B's intervals do not overlap
- Pros: closed form; behaves at small n and near 0 or 1; the rule is easy to state and to check.
- Cons: conservative. Non-overlap needs a larger gap than a test of the difference, so a real
  but small gain can read as none.

#### Option B: a bootstrap interval of the difference
- Pros: tighter.
- Cons: needs a seeded resampler and more explaining; at these n it adds little.

#### Option C: bare percentages
- Pros: simple.
- Cons: unreadable at n = 12.

#### Option D: one pooled claim, needing both Option A's non-overlap and a sign test over scenes
- Pros: the scene is the unit the sign test counts, so the 3 correlated repeats of a scene
  cannot pass for independent evidence; one comparison, so no family can be picked after the
  fact; stdlib only (`math.comb`).
- Cons: stricter than Option A; with many tied scenes (both settings always succeed or always
  fail) it needs a clear gain on the scenes that differ.

### 4. Order of the runs

#### Option A: all of A, then all of B
- Pros: fewest setting switches.
- Cons: a slow VM hour or a mid-session problem lands on one setting.

#### Option B: settings interleaved by engine run, scene order shuffled by a fixed seed
- Pros: time effects spread over both settings.
- Cons: none worth naming; the setting is per engine run anyway (`CELLRT_SETTING` is read once by
  the policy).

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option A for the splits, Option A for the checkpoint, Option D for the interval, and Option B for
the order. The plan had Option A for the interval; the gate review of this ADR (F15) found that it
treats the repeats of a scene as independent and allows a claim in every family, and Shaun
chose Option D on 2026-11-11.

**Families.**
- A family is a scene family of ADR 026 for one port type. Ten families can be drawn:
  `inrange_sfp`, `inrange_sc`, `yaw_edge_sfp`, `trans_edge_sfp`, `trans_edge_sc`, `beyond_sfp`,
  `beyond_sc`, `tilt_sfp`, `tilt_sc`, `empty_rail_sfp`.
- `nominal` is the control scene and `reversed` is pilot-only (D3); neither is a family here.

**The splits** (#80). For each split and each of the ten families, 4 scenes are drawn with
`random.Random(f"{split}:{family}:{i}")`:
- `inrange` families draw as in ADR 026.
- Families with a fixed value keep it and draw the rest:
  - `yaw_edge` draws the sign; `beyond` draws which of its values; `trans_edge` keeps ADR 026's
    end, the one its base does not sit at, on the base's rail;
  - each also draws the board pose within ADR 026's ranges.
- `tilt` draws its roll uniformly in [2°, 4°], plus the board pose.
- `empty_rail` draws which rail is left empty, plus the board pose, so that its scenes differ
  between splits.

The rules:
- `pilot-v1` keeps ADR 026's 23 scenes.
- No scene digest appears in two splits, except the two `nominal` scenes, which are the sample's
  own trials; they appear in the pilot and in tuning, never in the held-out.
- The three manifests, their engine configs and an interleaved `queue.json` are committed with
  their SHA-256 before any VM run.

**The checkpoint rule**, applied to the pilot's A trials. The pilot's B trials are picked by A's
outcome (ADR 026), so they do not count here.
1. Scenes marked "not applied", and infrastructure trials, are left out first.
2. A family qualifies if at least one of its remaining A trials was not compensated.
3. `inrange_sfp` (the control) and `empty_rail_sfp` (B cannot help) are always included, and
   do not count as qualifying: `empty_rail_sfp` can never be compensated.
4. Besides those two, at most 4 families. If more qualify, keep the 4 with the highest share of
   non-compensated A trials among their remaining A trials, so a family with more pilot scenes
   gains nothing from its size. Ties go in the order of the list above.
5. **If no other family qualifies (D7):** add `inrange_sc`, `tilt_sfp` and `tilt_sc`, the
   families of the natural SC jams and of tilt.
6. ADR 031 records the result and the pilot rows it was computed from.

**Tuning session** (#85):
- the selected families' `tune-v1` scenes, once with A and once with B: at most 6 × 4 × 2 = 48
  trials;
- clean repeats of the two `nominal` scenes, 6 per scene per setting: 24 trials;
- one forced VM stop mid-queue, then a resume.

**Calibration scope.**
- Measured from the tuning trials (#83): the force sample rate and its jitter, the force noise
  in free space, the tare residual at rest, and the latency from a force rise to a verdict.
- A change is allowed only under D5, recorded in ADR 032. Core defaults, `eval/baselines/` and
  the other one-way-door files are never changed.
- No change means no ADR 032, and the report says that nothing changed.

**Freeze.**
- The held-out session runs from a checkout of the tag `phase2-heldout`, set after tuning. A
  change under `cellrt/` or `adapters/` changes `config_hash`; a change under `tools/sim/` does
  not. So the ledger records the commit of every engine run (ADR 028), and the report refuses a
  held-out row from any commit other than the tag's.
- It must start by 2026-11-14 18:00. Otherwise it moves to after 2026-11-16, and nothing partial
  is reported as the eval.

**The held-out session** (#86):
- 4 `heldout-v1` scenes per selected family, A and B, 3 repeats each (seeds 0-2): at most
  6 × 4 × 2 × 3 = 144 trials.
- Each engine run is one setting, and consecutive engine runs alternate A and B. Within a
  setting, units are shuffled with `random.Random("heldout:order")`.
- C-LLM runs only on families where the pilot found an UNKNOWN verdict, with the same scenes and
  repeats, at most 50 model calls and 1 hour of VM time. Where it does not run, the report shows
  B's UNKNOWN count on the same families. C differs from B only on UNKNOWN, so a count of zero
  means C would have matched B.

**What is reported.**
- **The primary metric** is success as the referee sees it: the scorer's insertion into the
  target port (ADR 029).
- **Secondary metrics:**
  - the runtime's own success, and false successes;
  - recovery per ADR 029;
  - time over 20 N and the force penalty;
  - off-limit contacts;
  - duration;
  - the confusion matrix.
- **Wilson 95% intervals** (z = 1.96) per setting, pooled and per family. An undefined rate
  (n = 0) is shown as `-`.
- **One claim, pooled over the selected families.** B is said to beat A on the primary metric
  only if both hold:
  1. A's and B's pooled trial-level Wilson intervals do not overlap;
  2. an exact two-sided sign test over the held-out scenes gives p < 0.05. A scene where B
     succeeded in more of its 3 repeats than A counts for B, one where it succeeded in fewer
     counts for A, and ties are dropped.

  The same rule, the other way round, says A beat B. Otherwise the table reads "no detectable
  gain at this n".
- **Per family, and C against B, the numbers are descriptive.** Their intervals are shown, and
  no gain is claimed from them.
- **The budget:** tuning about 3.5 h (US$3.2); held-out about 6 h (US$5.4); C-LLM at most 1 h.
  The phase total is about US$12, under the US$25 ceiling (D8).

**After the freeze point** (2026-11-15 morning), results stay on the `phase2/results` branch as
a draft PR, out of the README, until Shaun reviews them.

## Why

- **Every choice that could bend the result is made before the data.** That covers the scenes,
  the families' selection rule, the tunable parameters, the interval and the claim rule. What
  is left after the pilot is arithmetic on a table. This is the answer to "did you tune it
  until B won?": the tuning scenes and the held-out scenes were committed, with hashes, before
  either ran.
- **Families, not scenes**, because a scene that happened to fail is noise, while a family that
  fails is a finding. The fixed controls make sure the eval always contains a case that should
  pass and a case B cannot fix.
- **A conservative claim rule, counted in scenes.** At these n a lenient rule would find gains
  in noise. The 3 repeats of a scene share its geometry, so they are not 3 independent pieces
  of evidence; the sign test counts each scene once. One pooled claim leaves no family to pick
  after the fact. The weakness to name first: the rule can miss a real but small gain. The
  report then says "no detectable gain at this n", which is true, instead of "no gain".
- **A deadline for the held-out**, because a half-run eval read as the result would be worse
  than none.

## Consequences

- ADR 031 (the held-out matrix) is written at the checkpoint, and is mechanical given this ADR.
- If a family proves broken in the pilot (every scene "not applied"), it cannot qualify, and
  its tuning and held-out scenes are never run.
- The results document (#86) states the splits, the hashes and the rule, so a reader can check
  that the reported scenes were fixed beforehand.
- To revisit: a test of the difference (Newcombe's interval) as a second view, if a later phase
  has the n for it.
