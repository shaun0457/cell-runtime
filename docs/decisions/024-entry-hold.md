# 024: Stopping above the port entrance before the plug meets it

Date: 2026-11-11
Status: accepted
Revised: 2026-11-11, after the first clean sim run with the stop: it now holds where the arm
is measured when it stops, not the z_offset last sent (Why, "It stops where the arm is").

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
  that is already there, adds at most 3 s and the steps down from where the arm stopped
  (about 0.7 s with the sim's lag) to a task with a 180 s limit (the sample config's
  `time_limit`), and keeps the descent's 0.5 mm steps. A realign after a retract from
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
- **What the stop does.** It stops where the arm is: it commands the z_offset measured at the
  stop (`z_offset_at`), not the one last sent, so it sends the arm no lower while the
  integrator keeps correcting XY (in the sim the arm still sank 3-5 mm in 8 of 9 stops; Why).
  It stops waiting once the plug has been within `ENTRY_XY_MM`
  (0.15 mm) of the axis for `ENTRY_SAMPLES` (3) samples in a row, counting the one at the stop,
  so one dip under the threshold does not end it. Otherwise it stops after `ENTRY_HOLD_S`
  (3 s) of the run's clock. Either way align then goes on down from the held z_offset in
  CheatCode's 0.5 mm steps to its usual end, z_offset 0.01. With the arm on its targets those
  are CheatCode's remaining offsets. A plug already on the axis costs two samples, 0.1 s.
- **What it records.** It judges nothing. Align records `entry_start_mm` and
  `entry_height_mm` (the tip's height when the stop begins and ends), `entry_xy_mm`,
  `entry_held_s` and `entry_converged`. Anything the plug then meets at the entrance is the
  Monitor's to classify.
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
  worse off than today's except for that time and the steps down from where the arm stopped.
  The Monitor and recovery still own the failure.
- **The thresholds are read from 13 runs, not tuned.** 0.15 mm sits inside the band
  (0.10-0.18 mm) where 3 of 4 plugs went in; the fourth, at 0.15 mm, stopped in the bore, which
  this does not claim to fix. 3 mm above the entrance was meant to keep the plug in free space
  while it moves sideways; in the sim the arm sank through it (the last bullet). The port
  models' entrance geometry was not measured, so this is a margin, not a clearance.
- **It is checked where it matters.** The fix is only proven by a clean setting-A sim run. If
  SC still fails, Phase 1's criterion is revised and the rate recorded, rather than tuned
  further: the time box Shaun set on 2026-11-11.
- **It stops where the arm is, because the first sim run showed the arm trailing.** The first
  version held the z_offset last sent. In the first clean setting-A run with it (one engine
  run with the code of `439e58b`, session `scclean` of the VM's `simclose.sqlite`, read from a
  copy), the sim's arm trailed its targets by 6-7 mm while it descended, so
  holding the target let it slide on down while XY converged. Every stop ended under its
  entrance: at 10.07 mm for SC (entrance 13.64 mm), at 41.9 and 43.5 mm for the two SFP tasks
  (45.8 mm). The SFP plugs still seated (scorer 75 each); the SC plug converged in 1.27 s to
  0.069 mm off the axis, but it had crossed the entrance during the stop 0.27 mm off, and it
  stopped in the bore at 5.15 mm (INSERT_STUCK, scorer 1). Holding the measured z_offset
  removes the part of that slide the trailing explains; a test drives an arm that trails its
  targets, with momentum, as the sim's did.
- **In the sim it slows the plug through the entrance rather than stopping it above.** The
  three clean setting-A runs after this revision (code of `0014a07`, same session; each
  align's `entry_*` fields and the samples during its stop) give these results:
  - In 8 of 9 stops the arm still sank while held at its own pose, by 3.1-5.1 mm over
    1.9-3.0 s, and the stop ended under the entrance. In those stops the plug went through
    the entrance while XY was corrected, sinking at 1-2.3 mm/s on average over the stop,
    against CheatCode's 10 mm/s. The ninth sank 1.1 mm in 0.1 s.
  - All six SFP plugs and two of three SC plugs seated (scorer 75 each). One SC plug
    crossed its entrance 0.97 mm off the axis and seated anyway.
  - The one SC plug that stuck is the one stop that ended above the entrance. It converged
    in 0.1 s at 15.5 mm, went on at full speed, was 0.36 mm off at the entrance, and stuck
    on the rim 0.70 mm off (13.46 mm, INSERT_STUCK). The error at the stop did not stay
    small over the last 3 mm, which the first Why bullet left open.

  So the stop does not do what the Decision set out to do in the sim. Why the arm sinks
  under a target at its own pose was not read from the controller (#73 review, F11).
  Three runs do not show which part helps. They are Phase 1's record, not a tuning set (the
  time box above).

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
- Only align stops. A descent that crosses the stop inside insert_scripted goes through the
  entrance without stopping, as before this change: a recovery that reruns insert_scripted
  without align (a retry, or the slow rerun after a FORCE_LIMIT in insertion), or an SC goal
  whose plug a dropout holds above the stop until align ends.
- After the stop, align's offsets are CheatCode's only when the arm was on its target. With
  the sim's lag they go down from the measured z_offset, 6-7 mm higher, so align sends more
  of them and ends later; insert_scripted's offsets are unchanged.
- The stop holds the measured z_offset, so a command dropout that ends while align is
  stopped no longer leaves the stop holding a target below the arm (#73 review, F1). One that
  ends before the stop is unchanged: align's next offset is as far below the arm as the steps
  lost, and the plug may cross the hold point and the entrance in that one move
  (`test_aic_faults.py`).
- The dropout fault meets the remaining limit, a stop only in align, only on an SC goal: its
  20 mm onset is above the SC stop (16.64 mm) and under the SFP stop (48.8 mm). The runbook's
  Phase 3 command injects it on every goal, as #61 did; the runs planned right after this
  change inject it on goal 1, an SFP goal, only.
