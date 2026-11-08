# 002: Time in the core

Date: 2026-11-08
Status: proposed

## Context

Skills have timeouts, the Monitor's FORCE_LIMIT rule sums the time over 20 N against a 1 s budget
(mirroring the AIC penalty), and the eval runs hundreds of units. Design principle 5 requires
that the same scenario and seed give the same result in mock mode. In Phase 2 the clock is
Gazebo's sim time, which also does not follow the wall clock.

## Options

### Option A — Simulated clock behind a `Clock` protocol
`SimClock` keeps an ordered heap of sleepers and only moves when the main loop calls
`advance(dt)` after each world step. Time is stored in integer nanoseconds. Skill timeouts are
checked against the clock; `asyncio.wait_for` / `asyncio.timeout` are not used.
- Pros: exact and reproducible (twenty 50 ms steps are exactly 1 s); a run of several seconds of
  cell time takes milliseconds, so 20 seeds × 3 settings × 7 scenarios is cheap; the same protocol
  wraps Gazebo sim time in Phase 2.
- Cons: every real-time API must stay out of the core, which needs a guard (ruff `banned-api`
  in `pyproject.toml`; `loop.time()` is a method call and is not caught); asyncio's own
  timeout helpers cannot be used; skills that block on anything other than the clock stall the
  loop (see ADR 003).

### Option B — Real asyncio time, with scaled-down constants
- Pros: idiomatic asyncio (`wait_for`, `sleep`); no clock abstraction.
- Cons: scheduling depends on machine load, so results are not reproducible; scaled constants
  (1 s → 10 ms) make CI flaky; long evals take real time.

## Decision

<!-- DRAFT by Claude from the code at 92d79e7 and the Phase 0 sessions. Shaun: rewrite
anything you would not say yourself, then set Status to accepted. -->

Option A. Time in the core comes only from an injected `Clock`. `SimClock` keeps integer
nanoseconds and moves only when the loop calls `advance(dt)` after a world step. Skill timeouts
are checked against the clock. Real-time APIs are banned in the core by ruff `banned-api`; the
two exceptions (`recorder.py`'s `wall_ts`, the drain's `asyncio.sleep(0)`) are listed per file in
`pyproject.toml`.

## Why

"Same seed, same result" is impossible if time comes from the machine: asyncio's timers depend
on load, so a timeout could fire on a busy CI runner and not on my laptop. With a simulated
clock, twenty 50 ms steps are exactly one second, and a 10 s insertion runs in milliseconds,
which is why 420 runs take under a minute.

It is also the right shape for Phase 2: Gazebo has its own sim time that does not follow the
wall clock, so the same protocol can wrap it. The cost is discipline (no `asyncio.wait_for`), and
I enforce it with a lint rule rather than a convention.

## Consequences

- Easier: deterministic tests and evals; long scenarios cost almost nothing.
- Harder: asyncio's timeout helpers are off-limits; `loop.time()` is a method call the lint rule
  does not catch.
- Revisit in Phase 1: a clock adapter over ROS sim time, and what the runtime does when sim time
  pauses or jumps (a Gazebo reset).
