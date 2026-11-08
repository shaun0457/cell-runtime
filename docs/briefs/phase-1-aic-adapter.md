# Phase 1 brief: the runtime drives the real AIC interface

Input for `to-spec`. It states the problem, the scope, the facts the design must respect and
the exit criteria. It does not make the open decisions: those are listed with their options and
belong in plan mode and ADRs. Facts are checked against `shaun0457/aic` @ `3ab0653`.

## Problem

Phase 0 proves the runtime on a mock world we designed. Nothing yet shows that the same
runtime can drive the AIC cell: real sim time, a real F/T stream, a controller that keeps
moving after a skill is cancelled, and an action interface we do not control. These integration
problems decide whether Phase 2's numbers mean anything, so they are solved before any fault is
injected in the sim.

## Goal

A `RuntimePolicy` loaded by `aic_model` runs the Phase 0 core against Gazebo and completes the
3 tasks of the AIC sample config with **no injected faults**, with every step recorded in the
event store and replayable with `cellrt replay`.

## In scope

- `adapters/aic/`: `RuntimePolicy`, an `AicWorld` (World protocol), a clock over ROS sim time,
  conversion from ROS messages to core types.
- Scripted skills built from CheatCode's logic: `approach`, `align`, `insert_scripted`,
  `verify`, `retract`, with Phase 0's skill contract and `monitor_profile`s.
- One stop path for every way a skill or task can end (see decision 2).
- One end-to-end run on the Spot VM with `ground_truth:=true`, and a runbook for it.

## Out of scope

- Fault injection in the sim, the A/B/C eval in the sim, the ACT skill (Phase 2).
- The LLM planner (Phase 3).
- Skill-level ROS 2 action servers (optional; `InsertCable` is already the action boundary).

## Pre-work (small tickets, before the adapter)

1. Add `uv.lock` and `adapters/` to `config_hash` (ADR 010 records the gap).
2. Add a schema version to `run_started`, so stores survive payload changes (#12).
3. Behavioural regression gate in CI: a small mock eval compared with a committed baseline;
   a change to the baseline file requires the `baseline-update` PR label.

## Facts the design must respect

| Fact | Where |
|---|---|
| A policy implements `insert_cable(task, get_observation, move_robot, send_feedback) -> bool` | `aic_model/aic_model/policy.py:137` |
| `insert_cable` runs in a thread started by the action's execute callback; the asyncio loop has to be created in that thread | `aic_model/aic_model/aic_model.py:236-255` |
| A cancel request is accepted, but the policy is not told: `insert_cable` receives no goal handle | `aic_model.py:183-185`, `:236-245` |
| `move_robot` switches the controller's target mode as needed and publishes the command | `aic_model.py:190-229` |
| `Policy.time_now()` and `sleep_for()` use the node clock, which follows sim time | `policy.py:79-88` |
| An `Observation` is published only when all three camera images have arrived within 1 ms of each other. **No camera images, no observations**: wrench and controller state stop too, so camera rendering cannot be switched off to save resources | `aic_adapter/src/aic_adapter.cpp:127-150` |
| The `wrist_wrench` in an `Observation` is the newest wrench at or before the image time: the F/T stream is subsampled to the camera rate | `aic_adapter.cpp:71-76`, `:213-224` |
| `wrist_wrench` is raw; subtract `controller_state.fts_tare_offset` (frame `ati/tool_link`, recomputed each cycle) per axis, then take the magnitude | CLAUDE.md, AIC facts |
| The scorer sums real time over 20 N across the task; -12 above 1 s | `aic_scoring/src/ScoringTier2.cc:863-903` |
| Motion modes: `MODE_VELOCITY` and `MODE_POSITION`; CheatCode uses position targets, ACT uses velocity | `aic_interfaces/aic_control_interfaces/msg/TrajectoryGenerationMode.msg` |
| A CheatCode task is about 32 s of sim time | `aic_example_policies/aic_example_policies/ros/CheatCode.py:221-255` |
| Heavy imports at module level make `aic_model` miss its GetState timeout; import lazily | `RunACT.py:17-33` |

## Integration decisions (plan mode; each non-trivial one becomes an ADR)

Issue #12 lists the open questions in full. The four below are the ones that shape the
architecture.

1. **Time and the loop.** Phase 0 advances the world and the clock itself (ADR 002, 003); in
   AIC, sim time runs on its own. How does the Clock protocol wrap `time_now` / `sleep_for`?
   How does the lockstep loop become tick → drain → wait one period of sim time → read the
   observation? Does the drain bound still hold? What happens when sim time pauses or resets?
2. **Stop path and cancellation.** Cancel, skill timeout, Monitor preemption, lifecycle
   deactivate and exceptions all go through one path that revokes the skill's command lease and
   sends HOLD (ADR 006). Since the policy is not told about a cancel, options include reading the
   goal state through the parent node, relying only on `Task.time_limit`, or another signal;
   each with its risk. In velocity mode the controller keeps the last twist, so "stopped" needs a
   definition and a confirmation.
3. **Force sampling.** Observations carry one wrench per camera frame. Options: subscribe to
   `/fts_broadcaster/wrench` directly and sum real dt per sample, or sum the dt between
   observations as an approximation. Whether the submission environment's Zenoh ACL allows the
   direct subscription is unknown; record it as an open item. Whatever is chosen, the result is
   compared with the scorer's number (exit criterion 5).
4. **Command mode for scripted skills.** CheatCode uses position targets; the Phase 0 mock and
   ACT use velocity. Position targets are safer to stop; velocity matches the mock's skill code.

## Exit criteria

Each is a behaviour a test or a recorded run can show.

1. Conversion tests run on Windows CI without ROS: tare per axis then magnitude, the tare frame,
   metres to millimetres, plug-to-port XY error and depth from ground-truth TF, `Velocity` or
   pose to `MotionUpdate`. Tests that need ROS skip when it is not importable.
2. `aic_model` loads `RuntimePolicy` without a GetState timeout.
3. The 3 sample-config tasks succeed, the same tasks CheatCode passes.
4. A cancelled goal stops the robot: a test with a fake node shows that every stop reason
   revokes the lease, sends HOLD, and records the reason; a late command from the old skill is
   dropped.
5. Time over 20 N from our log is compared with the AIC scorer's report for the same run; the
   difference and its cause are written down.
6. `cellrt replay` prints a sim run step by step, including the force curve.
7. A runbook lets the next VM session start the sim, load `RuntimePolicy` and find the log and
   the event store without guessing.

## Suggested first slice (tracer bullet)

A `RuntimePolicy` that runs one `approach` skill on one task: it reads observations, converts
them to `Signals`, commands one motion through `move_robot`, records events, and returns.
It touches every layer once (thread, loop, clock, conversion, command, store) before any
skill logic grows. Phase 0 started the same way (#4).

## Risks

- The sim on the VM runs slower than real time; time-based rules use sim time, but the wall
  time of a run grows. Measure the real-time factor in the first VM session.
- Observations depend on the cameras: a slow renderer slows the Monitor's sample rate.
- The ROS parts can only be tested on the VM, so the loop between writing and testing adapter
  code is slow. Keep the ROS layer thin and push logic into pure functions.
