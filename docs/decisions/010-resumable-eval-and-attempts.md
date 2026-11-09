# 010: How the eval resumes after it is killed

Date: 2026-11-08
Status: proposed

<!-- DRAFT by Claude: Context, Options and Consequences only. Decision and Why are Shaun's. -->

## Context

Phase 2 runs the eval on a GCP Spot VM, which can be preempted at any moment. Rerunning the same
`cellrt eval` command must pick up where it stopped and end with the same table as a run that
was never interrupted. CLAUDE.md fixes the frame: the unit of work is one (scenario, setting,
seed); a completed unit is recorded in the event store and skipped on restart; an interrupted
unit is rerun from the start, never resumed half-way. The store is append-only (ADR 004), so
nothing can be deleted or marked.

Current shape:

- Scenario ids must be unique in a scenarios file (`duplicate_scenario_id`), and `run_eval`
  refuses duplicates: every run, attempt and metric is keyed by them.
- Run ids are `scenario/setting/seed/aN`. `N` counts the unit's `run_started` events, so an
  interrupted attempt keeps its number and the rerun gets the next one.
- A unit is complete when one of its attempts has `run_finished` **and** that attempt's
  `config_hash` equals the hash of the configuration the eval runs now. The hash covers the
  parameters, a fingerprint of the core source (every `.py` under `cellrt/`, line endings
  normalised) and, since #20, of the adapter source (`adapters/`) and of `uv.lock`, so changed
  code or dependencies are a changed configuration. `run_started` also records a hash per part
  (`config_parts`: scenario, setting, world, monitor, skills, code, adapters, lock, recovery,
  planner), so the log
  shows which parts two runs share; the fairness test uses it to check that A, B and C share
  everything but recovery and planner.
- A unit interrupted `MAX_ATTEMPTS` (3) times under the current configuration is given up on:
  left out of the table and reported on stderr.
- If several finished attempts match, the first (lowest N) counts; the table reads exactly one
  run per unit.
- A test-only environment variable (`CELLRT_TEST_CRASH_AFTER_EVENTS=N`) makes the process exit
  with `os._exit` after its N-th event, without flushing: the test kills the eval mid-unit,
  reruns it, and compares the table with an uninterrupted eval; another kills the same unit
  twice and checks it finishes as `a3`.

## Options

### 1. What makes a unit complete

#### Option A — a finished attempt with the same `config_hash`, parameters and source (current)
- Pros: a code or parameter change can never reuse a stale result; old results stay in the
  store and are simply not counted; works without git, on a dirty tree, and gives the same
  value on Windows and Linux.
- Cons: any change under `cellrt/` (a comment included) reruns every unit; code outside
  `cellrt/` (adapters in Phase 1) is not covered, nor are dependency versions (a `py_trees` or
  `pydantic` upgrade in `uv.lock` reuses old results unless `uv.lock` is added to the hash).
  (Closed in #20: see Consequences.)

#### Option B — the same parameters plus the git commit (and a dirty flag)
- Pros: names the code version a reader can check out.
- Cons: needs git at run time; two different uncommitted edits look the same.

#### Option C — the same parameters only
- Pros: a refactor does not rerun anything.
- Cons: a changed Monitor rule or world model silently reuses old results (the first review of
  this ticket blocked on exactly that).

#### Option D — any finished attempt
- Pros: simplest; never reruns what already ran.
- Cons: after a change, the table silently mixes runs from two versions of the code.

#### Option E — a finished attempt from a named eval session (an eval id passed on the command)
- Pros: the person decides what belongs together.
- Cons: one more thing to get right by hand; a forgotten id mixes or repeats work.

### 2. Which finished attempt counts when there are several

#### Option A — the first (current)
- Pros: stable: rerunning never changes which run the table reads.
- Cons: a later, deliberate rerun (e.g. `cellrt run` by hand) is ignored.

#### Option B — the latest
- Pros: a manual rerun replaces the old result.
- Cons: the table can change between two evals with no change in the code.

### 3. An interrupted unit

#### Option A — rerun from the start as a new attempt (current, set by CLAUDE.md)
- Pros: one code path; no half-restored state; the mock is deterministic, so the rerun gives the
  same result as an uninterrupted one.
- Cons: work done before the kill is redone (a few seconds in mock, minutes per unit in sim).

#### Option B — resume from the last event (fork from step N)
- Pros: no repeated work.
- Cons: needs the world's state at step N, which the mock could rebuild but Gazebo cannot.

### 4. A unit that never finishes

#### Option A — give up after 3 interrupted attempts under one configuration (current)
- Pros: a unit that crashes the eval every time (a bug, not a preemption) cannot block the
  units after it on every restart; the eval says so on stderr.
- Cons: three real preemptions of one unit in a row look like a bug (unlikely: a unit takes
  seconds in mock, a couple of minutes in sim); the table then has one unit fewer.

#### Option B — no limit
- Pros: never drops a unit.
- Cons: with an automatic restart on the VM, one bad unit loops forever.

### 5. How the kill is tested

#### Option A — a test-only environment variable that makes the store exit abruptly (current)
- Pros: deterministic: the kill lands after an exact event count; no timing, no signals, works
  the same on Windows and Linux.
- Cons: a test hook in the core store; it skips only Python's cleanup, not the OS's (a real
  preemption can also lose writes SQLite has not synced).

#### Option B — kill the process from the test after a delay
- Pros: no hook in the code.
- Cons: where the kill lands depends on timing; flaky on a loaded CI runner.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

- In the AIC cell (Phase 1) a unit is one goal of an engine run: scenario
  `<session>/goal<N>/<task id>`, setting A, seed 0. `CELLRT_SESSION` names the engine config, so
  running it again gives new attempts of the same units. The engine sends no trial id, and in
  its sample config every trial's task is `task_1`; keyed by task id alone, the three trials
  would be three attempts of one unit. The cell never reruns a unit by itself. Two limits:
  two engine configs run under the same session share scenario ids, and goal N is the
  config's N-th task only when the engine sent every goal before it (a trial it skips because
  the model was not ready, or a task it never sends after a timeout, shifts the later goals).
  A Phase 2 runner that writes the engine configs can name the scenarios itself.

- An eval command is safe to rerun any number of times; `ran N units, M already finished` on
  stderr says what it did.
- The store grows with every interrupted or reconfigured attempt; nothing is ever removed, so
  a long campaign needs its own disk budget (the 20-seed Phase 0 eval writes about 34 MB).
- The runner does not yet shut the VM down when the queue is empty (CLAUDE.md, opt-in flag);
  that belongs with the Phase 2 runner.
- Listing the attempts builds the set of finished runs once (a non-correlated subquery), so
  starting an eval stays fast as the store grows; a test checks the query plan.
- The code fingerprint changes all config hashes whenever `cellrt/` changes: the first eval
  after any edit reruns everything (about 45 s in mock).
- Since Phase 1 (#20) the gap noted under Option A is closed: `config_hash` also has an
  `adapters` part (every `.py` under `adapters/`) and a `lock` part (`uv.lock`, line endings
  normalised), so an adapter change or a dependency upgrade reruns every unit too. A copy
  installed without them (no `adapters/`, no `uv.lock` beside the package) records a fixed
  "absent" value. Stores written before this change rerun once, since every hash changed.
  Such a copy therefore does not notice a dependency upgrade: evals are meant to run from a
  checkout (the VM clones the repo). `uv.lock` includes the dev group, so a ruff or pytest
  upgrade reruns everything too.
