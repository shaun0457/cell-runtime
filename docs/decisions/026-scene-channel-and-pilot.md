# 026: The scene channel and the fault-discovery pilot

Date: 2026-11-11
Status: accepted

**This decision changes what eval results mean.** It fixes which perturbations Phase 2 tries,
how each trial's outcome is classed, and which scenes get a B run. It is written before any
pilot data exists (pre-registration, part 1; part 2 is ADR 030).

## Context

Phase 2 asks what recovery is worth when the physics is not ours. Every sim run so far used the
engine's sample config. Two kinds of failure were seen:
- natural ones: the SC plug stuck on the entrance rim (3 runs) or in the bore (2 runs);
- an injected command dropout, which the Monitor calls UNKNOWN (#60, #61).

**How a scene can be varied without touching the runtime.** The engine takes
`aic_engine_config_file:=<abs path>` (`aic_bringup/launch/aic_gz_bringup.launch.py:752`), and the
eval container's entrypoint passes its arguments to `ros2 launch`. So a scene is an engine config
that we write. Per trial the config sets (`aic_engine/config/sample_config.yaml:56-140`):
- the task-board pose;
- which entity sits on each rail, its translation along the rail, and its roll, pitch and yaw;
- the cable's `gripper_offset` and roll, pitch and yaw, and the cable type (`sfp_sc_cable` or
  `sfp_sc_cable_reversed`).

**What the engine clamps.** Rail translation only (`aic_engine.cpp:1826-1828`, `:1856-1858`,
`:1888-1890`); rotations pass through (`:1832-1837`). The limits are read from the trial's own
node (`config_root = trial.config`, `:1790`), so the root-level `task_board_limits` block of the
sample config (`sample_config.yaml:46-55`) is never read. The hard-coded defaults apply
(`aic_engine.cpp:1791-1796`):

| Rail | Effective clamp (m) |
|---|---|
| NIC | [-0.048, 0.036] |
| SC | [-0.055, 0.055] |
| Mounts | [-0.09625, 0.09625] |

**What the challenge itself varies** (AIC docs):
- the board pose and yaw, with no range given (`docs/qualification_phase.md:22`, `:44`);
- the NIC card on one of 5 rails, with a translation range 0.062 m wide and yaw within ±10°
  (`docs/task_board_description.md`, zone 1);
- the SC port on one of 2 rails, with a translation range 0.115 m wide (zone 2);
- grasp deviations of about 2 mm and 0.04 rad (`docs/qualification_phase.md:20`).

The three sources disagree on translation. The docs give [0, 0.062] for NIC, the engine clamps
to [-0.048, 0.036], the sample's unread block states [-0.0215, 0.0234], and the sample's own
first trial sits at 0.036. Only the engine's clamp says what can actually be spawned.

**Why most perturbations will probably be compensated.** The skills read ground-truth geometry
every step (ADR 015, oracle mode), so a moved port or a shifted grasp is simply followed. Tilt
is the exception:
- `gripper_target` turns the plug to the port's orientation, but places it above the port
  origin's XY and offsets it along base z (`adapters/aic/geometry.py:130-134`);
- if the bore is tilted by θ, the entrance, at height h above the port frame, is h·tanθ to the
  side of that vertical line;
- SC (h = 13.64 mm): 0.48 mm at 2°, 0.95 mm at 4°. SFP (h = 45.8 mm): 1.6 mm at 2°;
- the SC rim jams seen so far were 0.35 and 0.97 mm off the axis at the entrance
  (`docs/sim-results.md`).

**What bounds the pilot.**
- One malformed trial stops the whole engine at parse time (`aic_engine.cpp:414-424`).
- `scoring.yaml` is written once, after the last trial (`:621`).
- The real-time factor is about 0.43 on the g2-standard-8 + L4 VM, at about US$0.9 an hour.

## Options

### 1. Which fault channel first

#### Option A: the scene channel only
- Pros: real physics; the challenge's own kind of variation; no runtime change.
- Cons: coarse (fixed for a whole trial); oracle geometry may compensate most of it.

#### Option B: perception error (a bias on the geometry the skills read)
- Pros: the likeliest to produce failures.
- Cons: a fault we design, in code under `adapters/`; Shaun deferred it until after the
  interview (D7).

#### Option C: both at once
- Pros: more chances to find something.
- Cons: twice the cost, and no clean answer to "does the challenge's own variation hurt?".

### 2. Which perturbations

#### Option A: draws inside the challenge's ranges only
- Pros: the most honest population.
- Cons: probably all compensated; nothing for B to do.

#### Option B: the ranges, plus their edges, a few values beyond them, tilt, and two unrecoverable cases
- Pros: puts trials where failure is plausible; gives B cases it cannot fix, so a gain cannot
  be manufactured by the scene list.
- Cons: the edge and beyond-range families are our choice; the report must say so.

#### Option C: a grid over every parameter
- Pros: systematic.
- Cons: far over the budget.

### 3. How many runs, and which scenes get B

#### Option A: A once per scene
- Pros: cheapest.
- Cons: cannot tell a scene that fails sometimes from one that fails always.

#### Option B: A twice per scene, then B once where A ended on a verdict that was not a false alarm, at most 12 (D2)
- Pros: B is spent only where there is something to recover.
- Cons: B's pilot numbers come from scenes picked by A's outcome, so they are not an estimate
  of B's rate; they only show whether B can act there.

#### Option C: A and B twice on every scene
- Pros: unbiased A/B comparison in the pilot.
- Cons: twice the cost, mostly spent on compensated scenes.

### 4. How a scene is recorded

#### Option A: as an injected fault (`fault_injected`) with a label
- Pros: reuses the mock's fault metrics.
- Cons: it lasts the whole trial and labels a family, so every verdict in the trial would be
  attributed to it, and classification accuracy would measure the scene list instead of the
  Monitor.

#### Option B: in the scenario part of the run's configuration, and in the manifest
- Pros: the scene is part of what the run is, like the board in a real cell; verdicts are
  judged against the referee (ADR 029), not against a label.
- Cons: the mock's fault metrics do not apply to scene runs.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option A for the channel (1), Option B for the perturbations (2), Option B for the runs (3), and
Option B for the recording (4).

**Base scenes.**
- SFP: the sample's `trial_1` (`nic_card_mount_0`, `sfp_port_0`).
- SC: the sample's `trial_3` (`sc_port_1`, `sc_port_base`).

The single-parameter families change one value of their base scene.

**The pilot manifest `pilot-v1`: 25 scenes.**

| Family | SFP | SC | Values |
|---|---|---|---|
| `nominal` | 1 | 1 | The base scene |
| `inrange` | 4 | 4 | Uniform draws of every parameter at once: board x ±0.02 m, y ±0.05 m and yaw ±0.14 rad around the base; grasp offset ±2 mm per axis and roll, pitch, yaw ±0.04 rad. SFP adds the NIC rail (0-4), the port (0 or 1), the translation inside the clamp and the yaw within ±10°. SC adds the rail (0 or 1) and the translation inside the clamp |
| `yaw_edge` | 2 | - | NIC yaw -10° and +10° |
| `trans_edge` | 2 | 2 | SFP: NIC translation -0.048 and +0.036 m. SC: `sc_rail_1` at +0.055 m, and `sc_rail_0` at -0.055 m, because the base already sits at -0.055 m on `sc_rail_1` |
| `beyond` | 3 | 1 | SFP: NIC yaw +20°; grasp offset x +5 mm; grasp roll +0.12 rad. SC: grasp offset x +5 mm |
| `tilt` | 1 | 2 | Roll of the target entity: SFP 2°; SC 2° and 4° |
| `empty_rail` | 1 | - | The target NIC rail left empty, the card on another rail |
| `reversed` | 1 | - | `sfp_sc_cable_reversed` on an SFP task, so the plug in the gripper is the wrong end |

The board pose has no published range; the ranges above are ours. They span the sample's own
trials, whose board x is 0.15 or 0.17 m and whose yaw is 3.0 or 3.1415 rad.

**Bounds that every generated scene and engine config must pass** (checked by
`validate_engine_config()`, #78):
- every translation lies inside the effective clamp, so the engine never moves a value silently;
- no trial carries its own `task_board_limits`, so the clamp stays the one above;
- the roll and pitch of every entity are at most 4°, and the board's are 0;
- every key that `Trial::Trial` reads is present (`aic_engine.cpp:41-240`).

**Runs.**
- A runs twice on every scene except `reversed`.
- `reversed` runs once with A, as the last unit of the queue, because the free plug can leave
  the sim in a state that later trials would inherit.
- That gives 49 A trials.
- B then runs once on each scene where A ended on a verdict that was not a false alarm
  (ADR 029). If more than 12 scenes qualify, the order is:
  1. scenes where both A trials qualify;
  2. scenes where one does;
  3. within each group, the manifest's order.

**Outcome classes, per trial**, using the counting of ADR 029:

| Class | The runtime | The scorer's insertion event |
|---|---|---|
| Compensated (on `nominal`: success) | Success, with no verdict | Inserted |
| Recovered | A verdict, then success | Inserted after the verdict |
| Natural failure, known class | A verdict of a known class; the run fails | Not inserted |
| Natural failure, UNKNOWN | An UNKNOWN verdict; the run fails | Not inserted |
| False alarm | A verdict | Inserted before the verdict |
| Adapter abort | Stopped by the adapter, with no verdict (`ground_truth_unavailable`, `hold_unconfirmed`) | Either |
| False success | Success | Not inserted |
| Infrastructure | No finished run | Rerun, as an attempt of the same unit |

- A verdict is a Monitor verdict or a skill's own failed result.
- An A run that stopped at a verdict, with no insertion, is a natural failure marked
  **unconfirmed**: A stopped, so nobody knows whether the plug would have gone in. The pilot's
  false-alarm count is therefore a lower bound.
- A scene whose two A trials fall in different classes is **mixed**.
- An insertion into the wrong port counts as not inserted, and the report names it.

**Recording.** A scene is not an injected fault: no `fault_injected` event. Its digest is part
of the run's scenario configuration, and the manifest holds its parameters (ADR 027).

**A perturbation that did not take effect.** `world_ready` (#79) reads back the spawned poses.
A single-parameter scene is checked against the nominal scene of its port type:

| Family | Check | Tolerance |
|---|---|---|
| `tilt` | The port's tilt from vertical equals the requested roll | 0.25° |
| `yaw_edge`, `beyond` yaw | The port's yaw changed by the request | 0.5° |
| `trans_edge` | The port moved by the request's magnitude | 1 mm |
| Grasp scenes | The plug's pose in the gripper frame changed by the request | 1 mm, 0.02 rad |

A scene outside its tolerance is "not applied", reported, and left out of every rate. The
`inrange` scenes combine several changes, so their readback is recorded but not checked.

**Cost ceiling.** US$6 for the pilot session (D8). The estimate:

| Item | Minutes |
|---|---|
| 49 A trials × 1.6 | 78 |
| 12 B trials × 2.5 | 30 |
| About 10 engine starts × 2 | 20 |
| VM setup | 15 |
| Bag dumps | 10 |

That is about 2.5 hours, or US$2.3. The runner refuses to start an engine run whose worst case
would pass the ceiling (ADR 028).

## Why

- **The scene channel is the cheapest way to ask the question honestly.** Its failures, if
  any, come from Gazebo's contact physics, not from a fault we wrote. The challenge varies
  exactly these parameters, so "the cell handles the challenge's own variation" is a claim
  worth testing, even if the answer is "trivially, because of oracle geometry".
- **The scene list is biased on purpose, and in both directions.** Edges, beyond-range values
  and tilt are where failure is plausible; the empty rail and the reversed cable are where B
  cannot help. A list of only easy scenes would show nothing. A list of only hard scenes would
  read as tuned for B. The weakness to name first: the edge and beyond-range values are ours,
  and an interviewer may ask why 20° and not 15°. The answer is cost: the pilot finds families
  that fail, and the held-out draws from those families (ADR 030), so no single value is
  load-bearing.
- **Not a fault event, because a scene is not an event.** A fault in the mock starts and ends,
  and the Monitor is scored on whether it caught it. A tilted port is there for the whole
  trial; the question is whether the run failed and whether B recovered it. The referee
  answers that (ADR 029).
- **Two A runs per scene** is the smallest number that can show a scene is sometimes fine and
  sometimes not. Gazebo is not bit-for-bit repeatable, and the rim jam already showed that.

## Consequences

- If every scene comes back compensated, that is the result (D7). The eval then uses the
  natural SC jams, the tilt family and the controls; the perception-error channel waits.
- The discovery report (#84) lists every scene, its class per trial and its sources (run id,
  `scoring.yaml`, bag JSON), plus the session's VM hours and cost from the ledger.
- The pilot's B numbers are not an estimate of B's success rate (Option 3B's con). Only the
  held-out eval (ADR 030) gives one.
- A scene the validator refuses never reaches the engine, so a typo cannot stop a whole engine
  run at parse time.
- To revisit after the interview: the perception-error channel, and tilt beyond 4° once the
  readback shows whether parts interpenetrate.
