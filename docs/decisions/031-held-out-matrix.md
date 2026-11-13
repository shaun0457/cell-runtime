# 031: The held-out matrix

Date: 2026-11-12
Status: accepted

**This decision changes what eval results mean.** It applies ADR 030's checkpoint rule to the
pilot and fixes the families that the tuning and held-out sessions run.

## Context

ADR 030 wrote the checkpoint rule before any data existed and left this ADR to record its
result, with the pilot rows it was computed from.

The pilot (`pilot1`) ran 12 engine runs at commit `6e80127`. Its report is
`docs/phase2/pilot1.md`. The checkpoint was posted on #84 before the tuning session started.

## Options

### Option A: apply ADR 030's rule as written
- Pros: anyone can redo the selection from the pilot table; no choice is made after the data.
- Cons: may keep a family that turns out to be dull, or drop one that would have been
  interesting.

### Option B: depart from the rule
- Pros: none that ADR 030 allows.
- Cons: reopens exactly the freedom the pre-registration closed.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option A. Step by step, on the pilot's A trials:

1. **Left out first.**
   - `beyond_sc_0` was not applied: a 5.00 mm grasp shift was asked for, and 10.30 mm turned by
     0.195 rad was read back, outside the 1 mm tolerance. Without it, `beyond_sc` has no scene
     left.
   - The pilot's one infrastructure trial was on `reversed_sfp_0`, which is not a family.
2. **Which families qualify.** A family qualifies if at least one of its remaining A trials was
   not compensated:

   | family | A trials | not compensated | share | result |
   |---|---|---|---|---|
   | `tilt_sc` | 4 | 4 | 1.00 | qualifies |
   | `tilt_sfp` | 2 | 2 | 1.00 | qualifies |
   | `inrange_sc` | 8 | 5 | 0.63 | qualifies |
   | `yaw_edge_sfp` | 4 | 1 | 0.25 | qualifies |
   | `beyond_sfp` | 6 | 1 | 0.17 | qualifies; dropped in step 4 |
   | `trans_edge_sfp` | 2 | 0 | 0.00 | does not qualify |
   | `trans_edge_sc` | 2 | 0 | 0.00 | does not qualify |
   | `beyond_sc` | - | - | - | no scene left (step 1) |
   | `inrange_sfp` | 8 | 2 | 0.25 | control (step 3) |
   | `empty_rail_sfp` | 2 | 2 | 1.00 | control (step 3); both trials adapter aborts |

3. **The two controls**, `inrange_sfp` and `empty_rail_sfp`, are always included.
4. **The cap.** Five families qualify. The four with the highest share are kept, and
   `beyond_sfp` is dropped.

**Selected:** `tilt_sc`, `tilt_sfp`, `inrange_sc`, `yaw_edge_sfp`, plus the controls
`inrange_sfp` and `empty_rail_sfp`.

**C-LLM does not run.** The pilot's one UNKNOWN verdict came on `nominal_sc`, which is the
control scene, not a family (ADR 030). So no selected family had an UNKNOWN, and C = B. The
held-out report shows B's UNKNOWN count on the selected families instead.

**The matrix:**
- **Tuning** (`tune-v1`): the six families' 4 scenes, once with A and once with B, 48 trials.
  Add the clean repeats of the two `nominal` scenes, 24 trials. 72 trials in all.
- **Held-out** (`heldout-v1`): the six families' 4 scenes, with A and B, 3 repeats each:
  144 trials, in 24 engine runs of 6.

**No calibration change.** Tuning measured the force signal. No trial showed one of the
thresholds that D5 allows to change (the AIC Monitor profiles and the AIC `RecoveryConfig`) to be
the cause of an outcome. So nothing was changed, and there is no ADR 032. The tag
`phase2-heldout` was set on `6e80127`, the commit the pilot and tuning had run.

## Why

- **The rule leaves no choice, and that is the point.** The pre-registration is only worth
  something if the step after the pilot is arithmetic. This ADR records the arithmetic so it
  can be checked.
- **Weakness to name first: the cap is blunt.** It dropped `beyond_sfp`, a family the skills
  mostly compensate. That costs little here. The same cap could, in another pilot, drop a
  family that mattered.

## Consequences

- The `beyond_*` and `trans_edge_*` scenes in `tune-v1` and `heldout-v1` never run.
- Phase 2 has no C-LLM numbers. The evidence for the model planner stays in Phase 3
  (`docs/phase3-llm-results.md`).
- The results are in `docs/phase2-results.md`.
