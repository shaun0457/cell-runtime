# Phase 2 results: recovery in the sim

Status: draft. The held-out session ran on 2026-11-12. The results stay on the `phase2/results`
branch and out of the README until Shaun reviews them (ADR 030).

## Summary

The held-out set held 24 scenes in six families, all fixed and hashed before any run: 144
trials in Gazebo, in oracle mode.
- With BT recovery (setting B), 34 of 72 plugs were inserted. With no recovery (setting A),
  28 of 72 were.
- The 95% Wilson intervals overlap: [0.36, 0.59] for B against [0.28, 0.50] for A.
- A sign test over scenes gives p = 0.34: B did better on 7 scenes, A on 3, and 14 were tied.

By the rule fixed before the run (ADR 030), the result is **no detectable gain at this n**.

- Recovery saved 10 trials, mostly on tilted ports and on the SC plug sticking at the port's
  entrance.
- In the held-out session the runtime never reported a success that the scorer did not see.
  Tuning had 2 such false successes (below).
- The Monitor raised no false alarm in 125 verdicts. That is a lower bound: 58 of them ended
  their run, so they could not be confirmed.
- The scorer's force penalty hit three trials whose runtime sum was at most 0.64 s.
  - The extra time came after the runtime's final FORCE_LIMIT verdict.
  - The force stayed over 20 N after the task stopped, and the runtime had stopped counting.
- The phase used at most 11 VM hours, about US$10, against a US$25 ceiling.

## What ran

All three sessions ran from one commit, `6e80127`. The held-out session ran from the tag
`phase2-heldout`, set on that commit. Nothing under `cellrt/` or `adapters/` changed between
the pilot and the held-out session.

| session | manifest | engine runs | trials counted | ledger hours |
|---|---|---|---|---|
| `pilot1` | `pilot-v1` | 12 | A 42, B 12 | 1.41 |
| `tune1` | `tune-v1` | 15 (2 not ok, below) | A 36, B 36 | 1.92 |
| `heldout1` | `heldout-v1` | 25 (1 not ok, below) | A 72, B 72 | 3.66 |

Ledger hours are the hours of the engine runs, summed from the ledgers. The reports print them
as "VM hours". The VM's own uptime is under Infrastructure below.

The manifests, as committed in `eval/sim/SHA256SUMS` before the first VM run (#80):

```
172241ca05c2fdc5514a4aea5f2c8ad6070b7110507c3adaf12b77df0127ee57  pilot-v1.json
ba8174d1e948968d4eb3bfbc3428d252210000bb94fbc11a0fd32b59fa5a0739  tune-v1.json
590fd652fe619328f1bedd7286cbd6aff6c1f31269ea7d00717f189142516c13  heldout-v1.json
c27a560d0421996e8965f20b10a4676bbb95d3601d6f75edbc5e15d7c5396359  queue.json
fce1ccbbb32ff79bbda73e9d5235da03fb1e227a7658191809dc8f5ed1938353  engine_template.yaml
```

The checkpoint (ADR 031) selected six families: `tilt_sc`, `tilt_sfp`, `inrange_sc` and
`yaw_edge_sfp`, plus the controls `inrange_sfp` and `empty_rail_sfp`. The pilot found no UNKNOWN
on a family, so C-LLM did not run, and C = B.

## Held-out result

Success means the scorer's insertion into the target port (ADR 029).

| family | A | B | B recovered | A mean s | B mean s |
|---|---|---|---|---|---|
| `tilt_sc` | 0/12 [0.00, 0.24] | 3/12 [0.09, 0.53] | 2 | 26.2 | 48.9 |
| `tilt_sfp` | 0/12 [0.00, 0.24] | 2/12 [0.05, 0.45] | 2 | 27.3 | 54.0 |
| `inrange_sc` | 7/12 [0.32, 0.81] | 9/12 [0.47, 0.91] | 4 | 28.7 | 33.8 |
| `inrange_sfp` | 9/12 [0.47, 0.91] | 9/12 [0.47, 0.91] | 0 | 28.7 | 39.1 |
| `yaw_edge_sfp` | 12/12 [0.76, 1.00] | 11/12 [0.65, 0.99] | 2 | 32.2 | 37.1 |
| `empty_rail_sfp` | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] | 0 | 10.0 | 10.0 |
| **pooled** | **28/72 [0.28, 0.50]** | **34/72 [0.36, 0.59]** | **10** | **25.5** | **37.1** |

- **The claim** (pooled, ADR 030): the intervals overlap, and the sign test gives p = 0.3438.
  So no detectable gain at this n.
- **Per family the numbers are descriptive.** Every family's A and B intervals overlap.
- **No trial was lost to infrastructure.**
- **Applied scenes:** 12 of the 24 scenes were read back as applied, and none as not applied.
  The other 12 could not be checked. The report's Applied section lists them.
- **Safety:** in the held-out session, no off-limit contact and no false success in either
  setting. One trial took the
  scorer's force penalty (below).

### The scorer counts more time over 20 N than the runtime does

FORCE_LIMIT fires when the runtime's own sum of time over 20 N reaches 0.5 s, half of the
scorer's 1 s. It fired in five trials across the three sessions:

| session | trial | setting | runtime's sum | scorer's force penalty |
|---|---|---|---|---|
| pilot | `inrange_sc_2_r0` | A | 0.504 s | −12 |
| tuning | `nominal_sc_r0` | A | 0.502 s | none |
| tuning | `tilt_sc_3_r0` | B | 0.640 s | −12 |
| held-out | `inrange_sfp_0_r1` | A | 0.520 s | −12 |
| held-out | `tilt_sfp_2_r1` | B | 0.520 s | none |

In three of the five, the scorer counted more than 1 s while the runtime counted at most 0.64 s.

**The cause is force after the runtime's last sample** (checked on 2026-11-13):
- **How it was checked:** the scorer's rule was replayed on the five trials' bags with a one-off
  script, outside the repository. The script takes the newest tare in bag order and sums the
  time between readings over 20 N. Each reading was then placed against the run's own times,
  mapped to sim time through `world_ready`.
- **The replay matches:** its totals equal the scorer's to 0.01 s, in these five trials and in
  four more.

Seconds over 20 N, by when the reading came:

| trial | before the goal | up to the runtime's last sample | after it, goal still running | after the goal | scorer's total |
|---|---|---|---|---|---|
| pilot `inrange_sc_2_r0`, A | 0.10 | 0.56 | 1.01 | 0.55 | 2.22 |
| tuning `nominal_sc_r0`, A | 0 | 0.52 | 0 | 0 | 0.52 |
| tuning `tilt_sc_3_r0`, B | 0 | 0.64 | 0.40 | 0.06 | 1.10 |
| held-out `inrange_sfp_0_r1`, A | 0 | 0.52 | 0.70 | 0.10 | 1.32 |
| held-out `tilt_sfp_2_r1`, B | 0 | 0.52 | 0 | 0 | 0.52 |

- **Up to the runtime's last `signal_sampled`, the scorer's time matches the runtime's sum,**
  within 0.06 s.
  - The runtime subscribes to the F/T topic itself and sums every reading by the scorer's rule
    (ADR 013).
  - The replay tares each reading with the newest `fts_tare_offset` in bag order, as the scorer
    does.
  - The two agree in that window, so the gap does not come from how the runtime samples or tares
    the force.
- **What happens after the last sample:**
  - In all five trials, the last sample is the run's final FORCE_LIMIT verdict. After it, the
    runtime drains no more readings.
  - In the three penalised trials:
    - the goal took 0.7 to 1.0 s more to return;
    - the force was over 20 N for 0.40 to 1.01 s of that time;
    - it was over 20 N for 0.06 to 0.55 s more after the goal ended;
    - it reached 346 N in the held-out one.
  - In the two without a penalty:
    - the goal returned 0.15 s after the verdict;
    - the force fell under 20 N at the verdict.
- **Counted only up to the verdict, all three stay under 1 s.** The verdict stops the task, but
  the force does not go away with it. The scorer sums every wrench reading in the trial's bag
  (`ScoringTier2.cc:205-243`, `:875-883`), including the ones after the goal.
- **In the pilot trial the force was over 20 N, at about 24 N, before the goal started.**
- **Not changed in Phase 2:**
  - backing off before the goal returns on a final FORCE_LIMIT;
  - draining the F/T readings until the goal returns, so the runtime's sum covers what the
    scorer counts.
- The report's A/B table shows `unsafe` 0 throughout, because that column counts trials where
  the runtime's own sum passed 1 s. The scorer's penalty is in the reconciliation table.

### Where recovery helped, and why mostly it did not

- **Tilted ports: 0/24 → 5/24 with B.** This follows from the skills' geometry:
  - The skills turn the plug to the port's orientation, then descend along the base's z axis
    to the port's origin (`adapters/aic/geometry.py`).
  - A port tilted by θ moves its entrance sideways by h·tanθ, where h is the entrance's height
    above the port frame. For SC (h = 13.64 mm), 2° to 4° is 0.48 to 0.95 mm. For SFP
    (h = 45.8 mm), it is 1.6 to 3.2 mm.
  - Retry and re-align descend along the same line, so they mostly meet the same rim.
  - Of B's 5 insertions on tilt, 4 were recovered after a verdict, and 1 went in without one.
    Over all six families, B's 87 INSERT_STUCK verdicts led to 14 recoveries. These are
    verdicts, not trials: a trial can have several, and 10 trials were recovered.

  This explanation comes from the code's geometry and was not tested on its own. The fix it
  points to, descending along the port's axis, is outside Phase 2.
- **`inrange_sc`: 7 → 9, with 4 recovered.** These are the natural SC jams on the entrance rim,
  the known limit from Phase 1 (`docs/sim-results.md`).
- **`inrange_sfp` and `yaw_edge_sfp`: B gained nothing.**
  - `inrange_sfp`: both settings inserted 9 of 12. B's recovery ran on its 3 failures and
    saved none of them.
  - `yaw_edge_sfp`: A 12 of 12 and B 11 of 12, with 2 of B's insertions recovered. At n = 12
    the gap is one trial.
- **`empty_rail_sfp`:** B cannot help, and it did not claim to.
- **Recovery costs time.** The mean task time went from 25.5 s to 37.1 s, and on the tilt
  families it roughly doubled.
- **Trials vary between repeats.** In B, 24 trials succeeded without a verdict, against 28 in A
  on the same scenes. Gazebo is not repeatable, and the SC grasp varies (below). The repeats of
  a scene also share its geometry, so they are not independent evidence. That is why ADR 030's
  sign test counts scenes, not trials.

## The Monitor

The held-out confusion table:

| setting | class | verdicts | false alarm | recovered | real failure | unconfirmed |
|---|---|---|---|---|---|---|
| A | INSERT_STUCK | 30 | 0 | 0 | 30 | 30 |
| A | FORCE_LIMIT | 1 | 0 | 0 | 1 | 1 |
| A | POSE_ERROR | 1 | 0 | 0 | 1 | 1 |
| B | INSERT_STUCK | 87 | 0 | 14 | 73 | 23 |
| B | TIMEOUT | 4 | 0 | 0 | 4 | 2 |
| B | FORCE_LIMIT | 2 | 0 | 0 | 2 | 1 |

- **False alarms: 0 of 125.** A verdict is judged against the time of the scorer's insertion
  event (ADR 029). An unconfirmed verdict is a real failure that its run stopped at, so 0 is a
  lower bound.
- **No UNKNOWN in the held-out session.** C would have matched B. Tuning had one UNKNOWN, on
  `tilt_sc_0` with B. It did not change the matrix, because ADR 030 decides C-LLM from the
  pilot alone.
- **False successes:** none in the held-out session. Tuning had 2, one in `tilt_sc` (B) and one
  in `yaw_edge_sfp` (A): `verify` judged the plug seated by its height, and the scorer saw no
  insertion.

## Calibration (measured only, ADR 030)

Over the 144 held-out trials (`docs/phase2/heldout1.md`, section Calibration):

| measure | value |
|---|---|
| force sample period | mean 50.3 ms, p95 52 ms, max 516 ms (the camera rate) |
| force in free space | mean 1.15 N, p95 1.64 N |
| force at rest above the entrance (SFP, during align) | mean 3.46 N |
| force rise to an INSERT_STUCK verdict | mean 0.49 s, max 0.51 s, 117 verdicts |
| FORCE_LIMIT: time over 20 N when it fires | mean 0.51 s, max 0.52 s, 3 verdicts |

By the runtime's own sum, FORCE_LIMIT fires with about half of the scorer's 1 s budget left,
as designed. The scorer counts more, as described above.

D5 allows changes only to the AIC Monitor profiles and the AIC `RecoveryConfig`. No tuning
trial showed one of those thresholds to cause an outcome, so nothing was changed, and there is
no ADR 032. The tuning false successes come from `verify`'s seated-height band, which D5 does
not cover.

One free-space SFP sample read 127.4 N. It has not been looked into.

## The SC grasp is not repeatable

Across the 12 clean `nominal_sc` trials of tuning, the plug's pose in the gripper's frame spread
by 9.6 mm and 0.214 rad. For SFP the spread was 0.10 mm.
- In tuning, `nominal_sc` inserted 3 of 6 with A and 3 of 6 with B.
- In the pilot, the spread was why `beyond_sc_0` was not applied. Its 5 mm grasp shift could not
  be told apart from the grasp's own variation.

The spread is larger than the grasp perturbations we designed, and it makes every SC rate noisy.

## The pilot

23 scenes, run with A twice (`docs/phase2/pilot1.md`):
- **Every tilt trial failed.** Recovery did not save any of them: B went 0/3.
- **`inrange_sc` failed often:** 5 of 8 A trials.
- **Compensated:** the edge translations, most SFP in-range scenes, and most yaw and grasp
  perturbations.
- **Monitor:** 0 false alarms in 27 verdicts. There was one UNKNOWN, on `nominal_sc`.

ADR 031 turns these rows into the held-out selection.

## Infrastructure

- **Forced stop.** The VM was stopped in the middle of `tune1-007`:
  - its ledger row reads `interrupted` (0.03 h), and no session lock was left;
  - its units were rerun from the start in later engine runs and counted once (ADR 028).
- **A failed bag dump hid off-limit contacts, in the pilot and in tuning.** Checked on
  2026-11-13.
  - `bag_dump.py` ran with only ROS's own setup sourced. The contact topic's type
    (`ros_gz_interfaces/msg/Contacts`) comes from the AIC overlay (`/ws_aic/install/setup.bash`).
  - **What the files show:** the five attempts the scorer gave −24 for a contact are exactly the
    five failed dumps. One more failed dump is left out of that count: `tune1-007`'s first
    trial, from the run the forced stop cut short, which has no scorer file.
  - **Why the dumps failed:**
    - in the nine bags replayed above, the contact topic is present in the four with a contact
      and absent in the five without;
    - in the eval container, the type loads only with the overlay sourced. This was checked by
      hand and not kept in a file.
  - A unit is complete only with its bag dump (ADR 028), so the runner reran each of those
    units:
    - **pilot:** both runs of `reversed_sfp_0` (A), where the wrist touched the enclosure. The
      pilot report lists the unit as "infrastructure (2 runs)", not as a contact.
    - **tuning, `inrange_sc_0_r0` B:**
      - attempt 1 (`tune1-006`): inserted, with a gripper finger on the SC port;
      - attempt 2 (`tune1-008`): failed, and also −12 for force;
      - attempt 3 (`tune1-010`) was counted: a success.
    - **tuning, `inrange_sc_1_r0` B:**
      - attempt 1 (`tune1-008`): failed, and also −12 for force;
      - attempt 2 (`tune1-010`) was counted: recovered.
  - **The held-out numbers are not affected:**
    - no held-out dump failed;
    - no held-out scorer file reports a contact.
  - **Tuning is descriptive:** one of its B successes (`inrange_sc_1_r0`) replaced a failed
    attempt.
  - **Not fixed in Phase 2:** sourcing the overlay for the dump, and keeping a failed dump as a
    result to look at instead of rerunning the unit.
- **Disk guard.**
  - The pilot's raw bags filled the 50 GB results disk.
  - The guard refused `tune1-002` and `heldout1-001` before either started, so no trial was lost.
  - The disk was grown to 200 GB, and the queue resumed.
- **Power-off.** The VM shut itself down when the held-out queue was empty.
- **Cost.**
  - The ledgers sum to 6.99 engine hours.
  - GCP's start and stop records give at most 11.0 VM hours. Two shutdowns from inside the VM
    are not recorded, so those sessions are counted up to the next start.
  - At US$0.90/h, that is at most US$9.9. Disk storage is not included.

## Limits

- **Oracle mode.** Ground-truth poses stand in for perception in every run (ADR 015).
- **No model calls in Phase 2.** C = B, and the LLM's evidence is Phase 3's.
- **The n is small.** Per-family intervals overlap, so the tilt and `inrange_sc` gains are
  descriptive.
- **The pilot and tuning reports leave out their five contacts.** A failed dump made the
  runner rerun those units (Infrastructure).
- **Not checked yet:** the 127.4 N sample above.
- **Deferred from the brief (#42):** a Spot preemption test (the forced stop above was on an
  on-demand VM), and ACT as `insert_act`.
- **Suggested by the tilt result, not in the brief:** descending along the port's axis.

## Reproduce

The runs ran at `6e80127`. The reports were generated afterwards with `tools/sim/report.py` at
`cbea14d` (branch `phase2/t6-calibration`, #94). That version adds the reference scenes and
the Calibration section; the one at the tag has neither. `tools/sim/` and `eval/sim/` arrive
with #88 to #94 and are not on this branch's base.

From each session's run directories:

```
python -m tools.sim.report --manifest eval/sim/pilot-v1.json --run DIR [--run DIR ...]
python -m tools.sim.report --manifest eval/sim/tune-v1.json --run DIR [--run DIR ...]
python -m tools.sim.report --manifest eval/sim/heldout-v1.json \
    --commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180 --run DIR [--run DIR ...] \
    --reference-manifest eval/sim/tune-v1.json --reference-run DIR [--reference-run DIR ...]
```

Their output is committed as `docs/phase2/{pilot1,tune1,heldout1}.md`. The run directories,
which hold the stores, ledgers, scorer files and referee JSON but not the raw bags, are kept
outside the repository. So are the force replay script and its output (2026-11-13). The raw
bags stay on the VM's results disk.
