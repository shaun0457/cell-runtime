# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

**cell-runtime** is an agent runtime for a robotic work cell: it makes a cable-insertion robot
*know when it has failed* and *recover*, and it measures how much that recovery is worth.

It runs on top of the AI for Industry Challenge (AIC) simulation (Gazebo, UR5e, ROS 2 Kilted).
The point of the project is **agentic infrastructure** — runtime, state, failure handling,
evaluation — not model quality. The robot policies are reused as they are; nothing is retrained.

Owner: Shaun Chen (shaun0457). This is a portfolio project: every design choice must be one he
can explain and defend in an interview.

### Non-goals

- No general-purpose agent framework. Build what this cell needs, nothing more.
- No model training, no Isaac Sim.
- The LLM never sits inside the real-time control loop.

## Design principles

1. **Known failures are handled deterministically.** The Monitor classifies a failure; if the
   class is known, the Behavior Tree handles it (retry, re-align, fall back to another skill).
   Only failures the Monitor cannot classify go to the LLM.
2. **Everything is an event.** Every step writes an append-only event; runtime state is a fold
   over events. Any run can be replayed, and a run can be forked from step N.
3. **The core is ROS-free.** `cellrt/` (runtime, skills interface, executor, monitor, planner,
   eval) must import nothing from ROS. ROS and AIC live only in `adapters/`. This keeps Phase 0
   testable on any laptop and keeps the design honest.
4. **LLM output is untrusted.** A tree or decision from the LLM is validated against a schema
   and the skill registry before it runs. Invalid output is an event, not a crash.
5. **Deterministic eval.** Same scenario + same seed = same result in mock mode.

## Architecture

```
LLM Planner      task -> BT; handles failures the Monitor cannot classify
    |
BT Executor      py_trees; retry, re-align, fallback skill
    |
Skills           approach / align / insert_scripted / insert_act / verify
    |
World            MockWorld (Phase 0)  |  AIC Gazebo via adapters/aic (Phase 1)

Monitor          force, progress, timeout -> FailureClass     (reads world signals)
Runtime          event log (SQLite), state = fold(events), tracing   (all layers write to it)
Eval harness     scenarios + fault injection -> metrics table          (reads the event log)
```

### Package layout (target)

```
cellrt/
  runtime/     events.py (event types), store.py (SQLite append-only), state.py (fold), clock.py
  skills/      base.py (Skill contract), registry.py, mock/ (Phase 0 skills)
  monitor/     signals.py, classifier.py (FailureClass)
  executor/    behaviours.py (py_trees <-> async skills), trees.py (tree builders per setting)
  planner/     base.py (Planner protocol), rule_planner.py, llm_planner.py (Phase 3), schemas.py
  world/       base.py (World protocol), mock_world.py
  eval/        scenarios.py, faults.py, metrics.py, runner.py, report.py, agent_report.py
  cli.py       `cellrt run | eval | baseline | replay | trace | smoke-llm | agent-report`
adapters/
  aic/         RuntimePolicy (an aic_model Policy plugin), AicWorld, skills on top of move_robot
eval/scenarios/   *.yaml
docs/decisions/   NNN-title.md (design decision records, written by Shaun)
tests/
```

### Skill contract

A skill is an async unit of work with an explicit contract:

- `name`, `preconditions(state) -> bool`, `async run(ctx) -> SkillResult`
- emits progress events while running (maps later to ROS 2 action feedback)
- must honour cancellation (`asyncio.CancelledError`) and a per-skill timeout
- returns `SkillResult(ok: bool, failure: FailureClass | None, detail: dict)`

`py_trees` is tick-based and synchronous; skills are async. A behaviour starts the skill as an
asyncio task on its first tick, returns RUNNING while the task runs, maps the result to
SUCCESS/FAILURE, and cancels the task on `terminate()`. Keep this bridge in one place
(`executor/behaviours.py`).

### Failure classes (initial)

`POSE_ERROR` (misaligned), `INSERT_STUCK` (force over threshold, no depth progress),
`FORCE_LIMIT` (tared force above 20 N, accumulated over the task — mirrors the AIC penalty;
the Monitor tracks the running total so recovery can act before the 1 s budget is spent),
`TIMEOUT`, `SENSOR_DROPOUT`, `UNKNOWN`. Injected faults carry a ground-truth label so
classifier accuracy can be measured.

### Eval settings

- **A** — no recovery: first failure ends the task
- **B** — BT recovery for known failure classes
- **C** — B + planner for UNKNOWN (rule-based stub in Phase 0, LLM in Phase 3)

Metrics: task success rate, pass^k, recovery rate, unsafe events (force-limit and off-limit
contact counts), mean duration and steps, planner calls per task, failure-classification accuracy.

## AIC facts this project depends on

Source: https://github.com/shaun0457/aic @ `3ab0653` (fork of intrinsic-dev/aic, Apache 2.0).
File:line references below are against that commit. Upstream may differ — check which checkout
you are reading before trusting a reference.

- A trial can contain several tasks. `aic_engine` sends one `InsertCable` action goal per task,
  in order, to the `aic_model` lifecycle node (`aic_engine.cpp:218-237`, `:1353`). The task
  sequence is owned by the engine config, not by us. Goals must be cancellable and finish within
  `Task.time_limit`.
- A policy implements `insert_cable(task, get_observation, move_robot, send_feedback) -> bool`
  (`aic_model/aic_model/policy.py`). Our AIC entry point is a `RuntimePolicy` with that signature.
- `Task`: `id, cable_type, cable_name, plug_type, plug_name, port_type, port_name,
  target_module_name, time_limit`.
- `Observation`: left/center/right images + camera info, `wrist_wrench`, `joint_states`,
  `controller_state`.
- **`wrist_wrench` is the raw `/fts_broadcaster/wrench` reading, not tared**
  (`aic_adapter.cpp:71-72`, `:224`). The tare offset is `controller_state.fts_tare_offset`,
  published in the sensor frame `ati/tool_link` and recomputed every control cycle
  (`aic_controller.cpp:1157-1160`, `:1275-1277`). Subtract it before applying any force
  threshold — this is exactly what the scorer does (`ScoringTier2.cc:423-429`, `:456`).
- An `Observation` is published only when all three camera images arrive within 1 ms of each
  other (`aic_adapter.cpp:127-150`): without camera images there are no observations at all, so
  rendering cannot be switched off. Its `wrist_wrench` is the newest wrench at or before the
  image time, so the F/T stream reaches the policy at the camera rate (`:213-224`).
- `insert_cable` runs in its own thread, and a cancel request is accepted without telling the
  policy: it receives no goal handle (`aic_model.py:183-185`, `:236-255`).
- Ground truth (launch with `ground_truth:=true`) arrives as TF frames:
  port `task_board/{target_module_name}/{port_name}_link`,
  plug `{cable_name}/{plug_name}_link`, gripper `gripper/tcp`, reference `base_link`.
- `CheatCode` (ground-truth expert) is two-phase: approach above the port (z_offset 0.2,
  interpolated), then descend in 0.5 mm steps with a PI correction on XY until z_offset < -0.015,
  then wait 5 s for the connector to settle. This is the basis for the scripted skills.
- A CheatCode task is about 32 s of **sim time** by construction: 100 x 0.05 s approach,
  430 descent steps x 0.05 s, 5 s settle (`CheatCode.py:221-255`). Wall-clock time depends on
  the real-time factor and can be several times longer on weak hardware. Large-N statistics
  still come from mock mode; sim runs are a smaller confirmation set.
- ACT policy: in this fork, `RunACT` loads `shaun0457/act_aic` from the local HF cache
  (`RunACT.py:52-53`; downloaded in `docker/aic_model/Dockerfile:30`). Upstream points to the
  organisers' `grkw/aic_act_policy` instead. State 26-dim, action = Cartesian twist, velocity
  mode, ~4 Hz. Import torch/lerobot lazily, as `RunACT._LightPolicy` does (`RunACT.py:17-33`):
  heavy module-level imports make `aic_model` miss its GetState timeout.
- Scoring (per trial, per `docs/scoring.md`; max 100): full insertion 75, wrong port -12,
  partial 38–50, proximity 0–25; off-limit contact -24; plus smoothness, duration, path length.
- **Force penalty (-12)**: the magnitude of the tared force (torque ignored) above 20 N,
  **accumulated** over the whole task for more than 1 s — not one continuous second
  (`ScoringTier2.cc:863-903`). Several short spikes add up.
- Scenario variation: engine config sets the task-board pose and per-rail translation.
  `task_board_limits` clamps **translation only** (`aic_engine.cpp:1798-1830`); rotations come
  from the scene config unclamped, so validate them ourselves.
- Ground-truth TF is for local development only: the submission environment blocks those
  topics with a Zenoh ACL (`docs/access_control.md`). Fine for this project; state it in the
  README.
- Runtime: ROS 2 Kilted, Pixi, eval in Docker. The official compose file connects the model to
  the eval router via `AIC_ROUTER_ADDR=eval:7447` (`docker/docker-compose.yaml:26-28`). In
  Shaun's earlier setup that did not connect, and `--network container:aic-eval-1` was the
  workaround — an environment issue, not a Zenoh limitation.

## Where things run

- **Phase 0–1: developed on Windows** (native Python 3.12, Shaun's Claude Code setup lives
  there), pushed to GitHub. The core must therefore run on both Windows and Linux:
  `pathlib` only, no `loop.add_signal_handler` or other POSIX-only APIs, no shell scripts
  in the core path. `.gitattributes` forces LF line endings.
- **CI**: GitHub Actions runs `ruff` + `pytest` for the core on `ubuntu-latest` and
  `windows-latest`. Adapter tests that need ROS/AIC are skipped when ROS is not importable.
- **Phase 2: GCP Spot VM** (n1-standard-8 + 1x T4), cloned from GitHub. Results and the
  SQLite store live on a persistent disk.
- **Spot VMs can be preempted at any time, so the eval runner must be resumable.** The unit of
  work is one (scenario, setting, seed). A completed unit is recorded in the event store and
  skipped on restart. A unit interrupted mid-run is rerun from the start, never resumed
  half-way. The runner shuts the VM down when the queue is empty (opt-in flag).

## Conventions

- Python >= 3.11 (Kilted on Ubuntu 24.04 ships 3.12). Type hints everywhere.
- Dependencies: stdlib `sqlite3` + `asyncio`, `py_trees`, `pydantic` (schemas), `typer` (CLI),
  `pytest`. Ask before adding anything else.
- Format with `ruff format`, lint with `ruff check`. Tests with `pytest -q`.
- Tests never call a real LLM; use a fake planner with scripted answers.
- Seeded randomness only (`random.Random(seed)` passed in); no global RNG.

## How to work with Shaun

- Start each phase in plan mode: propose the design, list the decisions and their trade-offs,
  and wait for approval before writing code.
- For every non-trivial decision, draft `docs/decisions/NNN-title.md` with the options and
  trade-offs only. Shaun writes the "Decision" and "Why" sections himself.
- Prefer small, reviewable commits per step. Explain what changed and why in the summary.
- Do not build ahead of the current phase.

## Status

Phase goals, exit criteria and the capability map are in `docs/roadmap.md`. Phase specs are
cut from `docs/briefs/`; open questions per phase are issues #12, #13 and #14.

- [x] Phase 0 — ROS-free runtime core + mock world + eval CLI (ADRs 001–010: Decision and
  Why still to be confirmed by Shaun)
- [ ] Phase 1 — AIC adapter and scripted skills; the 3 sample-config tasks end to end with no
  faults, recorded and replayable (`docs/briefs/phase-1-aic-adapter.md`)
- [ ] Phase 2 — fault injection, ACT as `insert_act`, the A/B/C eval in Gazebo, reconciled with
  the AIC score
- [ ] Phase 3 — LLM planner: UNKNOWN triage, task-to-tree generation, escalation, and UNKNOWN
  promotion validated by replay (`docs/briefs/phase-3-unknown-promotion.md`)
- [ ] Phase 4 — eval report, README, demo video
