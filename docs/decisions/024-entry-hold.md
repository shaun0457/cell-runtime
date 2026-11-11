# 024: Stopping above the port entrance before the plug meets it

Date: 2026-11-11
Status: accepted

## Context

Phase 1's exit criterion is the 3 sample-config tasks in the sim with no faults. The two SFP
tasks pass since #58. The SC task (goal3) has failed in every clean setting-A run so far:
#28, #61's clean run and the F1 rerun's clean run. The Monitor called INSERT_STUCK each time:
twice with the plug on the SC entrance rim, 13.5 mm above the port frame, and once with it
stopped in the bore at 5.16 mm (#61's clean run).

Thirteen goal3 runs are on record, in copies of the #61, F1-rerun and 4096-token stores. They
were read again for this ADR:

- Every align ran its whole descent (about 19 s, ending about 24.4 s into the run) and ended
  within 1 mm of the axis. Whether the plug went in was settled **during align's descent**,
  before insertion and before any Monitor rule.
- The plug's distance from the port axis as it reached the entrance (the first sample at or
  under 14 mm) sorts the outcomes, though not cleanly:

  | off the axis at the entrance | runs | outcome |
  |---|---|---|
  | 0.10-0.18 mm | 4 | 3 seated during align, 1 stopped in the bore at 5.15 mm |
  | 0.34-0.42 mm | 4 | 1 seated, 1 stopped in the bore, 2 stuck on the entrance rim |
  | 0.50-0.87 mm | 5 | all 5 stuck on the rim |

- On the rim the XY error stops changing and the force climbs to about 13 N. The plug presses
  on the rim, and friction holds it where it landed.
- On the way down, the error swings between 0 and 0.9 mm. CheatCode's descent steps 0.5 mm
  every 50 ms and corrects XY only through its integral term (gain 0.15), so the error at the
  moment the plug reaches the entrance is a matter of where the swing happens to be.
- Align already waits for the axis: at z_offset 0.01 it holds until the plug is within 1 mm.
  Because of CheatCode's tip-offset quirk, z_offset 0.01 is far below the entrance. By then
  the plug is already seated, stopped in the bore, or stuck on the rim, so the wait does
  nothing. This is the
  "align/insert boundary" the #28 report left open.

CheatCode, whose descent this is, seated the SC plug in its one #28 run. One run does not show
that the descent is reliable.

Usual practice: a peg-in-hole insertion aligns above the hole before the chamfer, and only then
moves down; a guarded move then stops on contact. Industrial insertion cycles add a search
(spiral or tilt) when the first contact misses. That is a contact strategy, and the
`insert_act` and model-written programs belong to that later work, not to this fix.

## Options

### A — keep CheatCode's descent; let recovery handle the jam
- Pros: no change to a ported expert; recovery has seated the plug after a jam once
  (F1 rerun, C-LLM #3).
- Cons: setting A has no recovery, so Phase 1's criterion stays unmet. Retract-and-realign
  then failed twice in a row in another run.

### B — stop once above the entrance and correct XY until the plug is on the axis
Place the stop by the port type's measured entrance. The wait has a time limit, and when the
limit runs out the descent goes on.
- Pros: it targets the cause the data shows, the error at the entrance. It uses the integrator
  that is already there, adds at most 3 s to a task with a 180 s limit (the sample config's
  `time_limit`), and keeps the descent's offsets as they are. A realign after a retract from
  the rim crosses the stop again, so the recoveries that run align again (retract and realign,
  a slowed realign) gain from it too.
- Cons: it is the first departure from "run once at full speed, every skill commands what
  CheatCode does". The thresholds come from 13 runs. It does nothing for a plug that stops in
  the bore (2 of 13 runs).

### C — slow the whole descent near the entrance
- Pros: smaller swings everywhere near the entrance.
- Cons: the swing comes from the integral term's lag, so a slower descent shrinks it but does
  not settle it. It also stretches every task and changes every offset after the entrance.

### D — a contact search at the rim (spiral, wiggle)
- Pros: it is what a real cell would add, and it also covers a miss that slips through.
- Cons: it is a new skill with force control that the scripted skills do not have. It is
  larger than Phase 1 needs, and it is what setting D (a model-written program) or ACT would
  explore.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option B.

- **Where the stop is.** Align stops the first time the tip is at most `ENTRY_HOLD_ABOVE_MM`
  (3 mm) above its port type's entrance: 16.64 mm for SC, 48.8 mm for SFP. An align that starts
  below that point does not stop. That covers one already in the port, such as a realign after
  a retract from inside the bore.
- **What the stop does.** It commands the z_offset it reached again, so the integrator keeps
  correcting XY. It stops waiting once the plug has been within `ENTRY_XY_MM` (0.15 mm) of the
  axis for `ENTRY_SAMPLES` (3) samples in a row, counting the one at the stop, so one dip
  under the threshold does not end it. Otherwise it stops after `ENTRY_HOLD_S` (3 s) of the
  run's clock. Either way it then goes on with CheatCode's remaining offsets. A plug already on
  the axis costs two samples, 0.1 s.
- **What it records.** It judges nothing. Align records `entry_xy_mm`, `entry_height_mm` (the
  tip's height when the stop ends), `entry_held_s` and `entry_converged`. Anything the plug
  then meets at the entrance is the Monitor's to classify.
- **Seated bands per port type.** The same `PortProfile` that places the stop also gives each
  port type its seated band (#70). SFP is -1.5 to -0.6 mm. SC is -0.5 to +0.5 mm.
- **An unknown port type.** A port type with no profile is refused before anything runs, as
  `UnknownPortType`, so the policy's log names the cause. The engine sent `sfp` and `sc` in
  every recorded run.
- **It applies to SFP too.** One rule for both types: an SFP align stops at 48.8 mm. The SFP
  tasks pass today, so the sim run must show they still do.

## Why

- **It acts on the cause the data shows.** The data says the error at the moment of contact
  decides the outcome. The stop is the cheapest way to make that error small, and it uses the
  same correction the descent already has. What it does not show yet is that the error stays
  small over the last 3 mm, about 6 steps of descent after the stop. `entry_xy_mm` is the
  error when the stop ends, not at contact. The sim run reads the error at contact from the
  recorded samples (the first `signal_sampled` at or under 14 mm for SC). That reading
  confirms the mechanism or rules it out, whatever the outcome.
- **A stop that cannot converge does no harm.** It goes on after 3 s, so the run is never
  worse off than today's except for that time. The Monitor and recovery still own the failure.
- **The thresholds are read from 13 runs, not tuned.** 0.15 mm sits inside the band
  (0.10-0.18 mm) where 3 of 4 plugs went in; the fourth, at 0.15 mm, stopped in the bore, which
  this does not claim to fix. 3 mm above the entrance keeps the plug in free space while it
  moves sideways. The port models' entrance geometry was not measured, so this is a margin,
  not a clearance.
- **It is checked where it matters.** The fix is only proven by a clean setting-A sim run,
  planned right after this change. If SC still fails, Phase 1's criterion is revised and the
  rate recorded, rather than tuned further: the time box Shaun set on 2026-11-11.

## Consequences

- AIC skill configs change, so the config hash changes, and runs before and after are
  different configurations. The mock world and the committed baseline are untouched.
- Each skill's config records the whole port table, not the profile its task uses. The
  profile follows from the task's port type, which is in the scenario part. So the SFP and SC
  goals of one engine run stay one configuration, and the agent report can pool them.
- `skills.py`'s module docstring no longer says the skills command exactly what CheatCode
  does. It names this one addition.
- The thresholds and the bands are measured values for two port types. A third type needs
  measuring first (`port_profile` refuses it).
- A plug that stops in the bore (about 5.15 mm, 2 of 13 runs) is not addressed.
- Only align stops. A recovery that runs insert_scripted again without align (a retry) goes
  through the entrance without stopping, as before this change.
- The stop commands the z_offset last sent, as the rest of the descent does. While every
  command arrives, that is where the arm is. After a command dropout during align it can be
  below the arm, so the stop may then sit at or under the entrance. The planned sim runs
  inject the dropout on an SFP goal at 20 mm, under the SFP stop (48.8 mm), so they do not
  meet this.
