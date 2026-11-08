# 014: Command mode for scripted skills, and what HOLD means in each world

Date: 2026-11-09
Status: proposed

## Context

Phase 0 skills command a Cartesian velocity (`Velocity`, the shape of the AIC twist and of the
ACT policy's action), and the executor sends `HOLD` (zero velocity) whenever a skill ends or is
stopped (ADR 006). Phase 1 drives the AIC controller through `move_robot`, which takes a
`MotionUpdate` with either a pose target or a velocity target
(`TrajectoryGenerationMode.MODE_POSITION` / `MODE_VELOCITY`). CheatCode, the expert the scripted
skills are built from, uses position targets in 0.05 s steps
(`aic_example_policies/ros/CheatCode.py:221-255` @ `3ab0653`). The controller keeps applying
the last velocity target until it gets a new one, so in velocity mode "stopped" depends on a
zero twist actually arriving.

Two questions follow: what the scripted skills command, and what HOLD becomes in the cell. The
core must stay ROS-free, and the mock results must not change.

Current shape (since #23, the plan's default): the world command is `Command = Velocity |
PoseTarget`. `PoseTarget` is ROS-free (position in mm, orientation as a quaternion in the base
frame). The mock world accepts velocities only and raises `UnsupportedCommand` on a pose target,
which the executor records as a skill error. `HOLD` stays the zero velocity in the core; each
world says what it means.

## Options

### What the scripted skills command

#### Option A — Cartesian position targets, as CheatCode does (current default)
- Pros: the 3 sample tasks are known to succeed this way (CheatCode passes them); stopping is
  safe by construction (the arm settles on the last target instead of carrying on); targets are
  absolute, so a late or repeated command does not accumulate motion.
- Cons: the scripted skills' code differs from the mock skills' code (velocity), so Phase 0's
  skills are not reused as they are; a target too far from the current pose makes the
  controller's own trajectory generation decide the motion.

#### Option B — Cartesian velocity, as the mock skills and ACT do
- Pros: one command type for every skill; the mock skills' control laws could be ported
  directly; matches what `insert_act` (Phase 2) will send.
- Cons: a stopped skill leaves the last twist applied until HOLD arrives, so every stop path
  depends on a message getting through; drift accumulates with sample-rate jitter; no proven
  run on the sample tasks.

#### Option C — both, chosen per skill
- Pros: approach and retract in position mode, contact phases in velocity (or the reverse).
- Cons: mode switches in the middle of a task (`aic_model` calls `ChangeTargetMode` on each
  switch, `aic_model.py:190-229`); two control laws to tune and explain.

### What HOLD means in the cell

#### Option A — a position target at the measured TCP pose (`controller_state.tcp_pose`) (current default)
- Pros: also cancels a residual twist, since the controller leaves velocity mode; the arm
  stays where it is even if a later message is lost.
- Cons: the measured pose lags the true pose by one controller cycle, so the arm may settle a
  little back; switches the target mode when the last skill used velocity.

#### Option B — a zero twist in velocity mode
- Pros: no mode switch; the same meaning as in the mock.
- Cons: relies on the zero twist arriving; the impedance controller may still drift under
  contact force.

#### Option C — position target at the last commanded target
- Pros: no dependence on the measured pose.
- Cons: in velocity mode there is no last target; after a preemption the last target may be
  the very motion that caused the failure.

### Where the command type lives

#### Option A — a union in the core, `Velocity | PoseTarget` (current default)
- Pros: one world protocol for the mock, the AIC world and later ACT; the executor's lease and
  HOLD logic stay unchanged; a pose target sent to the mock is a recorded error, not silent.
- Cons: the core names a command the mock cannot execute.

#### Option B — pose targets only in the adapter (AIC skills get a richer world view)
- Pros: the core stays exactly as in Phase 0.
- Cons: two command paths, and the lease would have to guard both.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

- The mock's behaviour is unchanged: the 20-seed Phase 0 table is identical before and after
  #23.
- A pose target reaching the mock world ends the run as "<skill> raised UnsupportedCommand".
- HOLD's meaning in the cell is implemented in the AIC world (#24, #25), not in the core.
