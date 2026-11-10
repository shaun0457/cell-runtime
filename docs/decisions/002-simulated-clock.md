# 002: Time in the core

Date: 2026-11-08
Status: accepted

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

<!-- Decision and Why drafted by Claude from the option the code took. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option A. Time in the core comes only from an injected `Clock`. `SimClock` keeps integer
nanoseconds and moves only when the loop calls `advance` after a world step. Skill timeouts are
checked against the clock. Real-time APIs are banned in the core by ruff `banned-api`; the
exceptions are listed per file in `pyproject.toml`.

## Why

"Same seed, same result" is impossible if time comes from the machine: asyncio's timers depend
on load, so a timeout could fire on a busy CI runner and not on the laptop. With a simulated
clock, twenty 50 ms steps are exactly one second, and a 10 s insertion runs in milliseconds,
which is why 420 runs take under a minute.

It also carried over to the sim without a second clock: Phase 1 keeps `SimClock` and advances
it by the sim time each step reports (ADR 011). The cost is discipline (no `asyncio.wait_for`),
enforced by a lint rule rather than a convention.

## Consequences

- Easier: deterministic tests and evals; long scenarios cost almost nothing.
- Harder: asyncio's timeout helpers are off-limits; `loop.time()` is a method call the lint rule
  does not catch.
- Settled in Phase 1 (ADR 011): in the cell the same clock is advanced by the sim time each step
  reports, so a paused or reset sim is handled by the adapter, not by a second clock.
