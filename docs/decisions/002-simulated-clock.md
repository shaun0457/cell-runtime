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

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.
