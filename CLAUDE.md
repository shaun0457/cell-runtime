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
World            MockWorld (Phase 0)  |  AIC Gazebo via adapters/aic (Phase 2)

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
  eval/        scenarios.py, faults.py, metrics.py, runner.py, report.py
  cli.py       `cellrt run | eval | replay`
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
`FORCE_LIMIT` (force > 20 N for > 1 s — mirrors the AIC penalty), `TIMEOUT`, `SENSOR_DROPOUT`,
`UNKNOWN`. Injected faults carry a ground-truth label so classifier accuracy can be measured.

### Eval settings

- **A** — no recovery: first failure ends the task
- **B** — BT recovery for known failure classes
- **C** — B + planner for UNKNOWN (rule-based stub in Phase 0, LLM in Phase 3)

Metrics: task success rate, pass^k, recovery rate, unsafe events (force-limit and off-limit
contact counts), mean duration and steps, planner calls per task, failure-classification accuracy.

## AIC facts this project depends on

Source: https://github.com/shaun0457/aic (fork of intrinsic-dev/aic, Apache 2.0).

- One trial = one `InsertCable` action goal sent by `aic_engine` to the `aic_model` lifecycle
  node. The trial sequence is owned by the engine config, not by us. Goals must be cancellable
  and finish within `Task.time_limit`.
- A policy implements `insert_cable(task, get_observation, move_robot, send_feedback) -> bool`
  (`aic_model/aic_model/policy.py`). Our AIC entry point is a `RuntimePolicy` with that signature.
- `Task`: `id, cable_type, cable_name, plug_type, plug_name, port_type, port_name,
  target_module_name, time_limit`.
- `Observation`: left/center/right images + camera info, `wrist_wrench` (F/T, tared at start),
  `joint_states`, `controller_state`.
- Ground truth (launch with `ground_truth:=true`) arrives as TF frames:
  port `task_board/{target_module_name}/{port_name}_link`,
  plug `{cable_name}/{plug_name}_link`, gripper `gripper/tcp`, reference `base_link`.
- `CheatCode` (ground-truth expert) is two-phase: approach above the port (z_offset 0.2,
  interpolated), then descend in 0.5 mm steps with a PI correction on XY until z_offset < -0.015,
  then wait for the connector to settle. This is the basis for the scripted skills.
- ACT policy: Hugging Face `shaun0457/act_aic` (LeRobot ACT). State 26-dim, action = Cartesian
  twist, velocity mode, ~4 Hz. Import torch/lerobot lazily (see `RunACT._LightPolicy` in the AIC
  repo): heavy module-level imports make `aic_model` miss its GetState timeout.
- Scoring (per trial, max 100): full insertion 75, wrong port -12, partial 38–50, proximity 0–25;
  force > 20 N for > 1 s: -12; off-limit contact: -24; plus smoothness, duration, path length.
- Scenario variation: engine config sets task-board pose and per-rail translation/rotation
  within `task_board_limits` — use this for scenario generation in Phase 2.
- Runtime: ROS 2 Kilted, Pixi, eval in Docker. Zenoh does not connect across docker-compose
  services: run the model container with `--network container:aic-eval-1`.
- A CheatCode trial takes minutes of sim time (settling alone is long). Large-N statistics come
  from mock mode; sim runs are a smaller confirmation set.

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

- [ ] Phase 0 — ROS-free runtime core + mock world + eval CLI
- [ ] Phase 1 — AIC adapter: `RuntimePolicy` plugin, same eval on the real interface
- [ ] Phase 2 — AIC Gazebo: scripted skills on ground truth, ACT as `insert_act`, fault injection
- [ ] Phase 3 — LLM planner (validated trees, UNKNOWN-failure triage, human escalation)
- [ ] Phase 4 — eval report, README, demo video
