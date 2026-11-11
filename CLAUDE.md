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
   over events. Any run can be replayed, and the state at any step can be rebuilt (`state_at`).
   Forking a run from step N is not built; `state_at` is its basis.
3. **The core is ROS-free.** `cellrt/` (runtime, skills interface, executor, monitor, planner,
   eval) must import nothing from ROS. ROS and AIC live only in `adapters/`. This keeps Phase 0
   testable on any laptop and keeps the design honest.
4. **LLM output is untrusted.** A tree or decision from the LLM is validated against a schema
   and the skill registry before it runs. Invalid output is an event, not a crash.
5. **Deterministic eval.** Same scenario + same seed = same result in mock mode.
6. **Ground truth has three roles** (ADR 015). Referee labels (the scorer's insertion events,
   injected fault labels) are read only by the offline eval, never online. Oracle geometry (GT
   poses standing in for perception) may be used in an oracle mode named as such. Measured
   observations (wrench, TCP pose, joint states, images) are the runtime's own. A model's
   judgement is a prediction scored against the referee. Every AIC run so far is in oracle mode.

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

### Package layout

As built through Phase 3; a later phase adds to it.

```
cellrt/
  runtime/     events.py (event types), store.py (SQLite append-only), state.py (fold),
               recorder.py, clock.py, task.py, scrub.py (provider text kept out of the log)
  skills/      base.py (Skill contract), registry.py, mock/skills.py (the mock world's skills)
  monitor/     classifier.py (FailureClass), profiles.py, promoted.py (promoted rules)
  executor/    behaviours.py (py_trees <-> async skills), trees.py (tree builders per setting),
               runner.py (the lockstep loop), recovery.py, samples.py, wall.py
  planner/     base.py (Planner protocol), rule_planner.py, fixed_planner.py, llm.py (model
               triage), task_plan.py + llm_task.py (task-to-tree), nim.py (NIM client),
               factory.py, schemas.py, smoke.py
  promotion/   groups.py, propose.py, replay.py, validate.py, adopt.py (UNKNOWN promotion)
  world/       base.py (World protocol, Signals), mock_world.py, faults.py
  eval/        scenarios.py, faults.py, metrics.py, runner.py, report.py, agent_report.py,
               baseline.py, referee.py (sim verdicts against the scorer, ADR 029),
               intervals.py (Wilson intervals and the sign test, ADR 030)
  replay.py, trace_export.py
  cli.py       `cellrt run | eval | baseline | replay | trace | smoke-llm | agent-report |
               promote group | validate | approve | propose`
adapters/
  aic/         RuntimePolicy.py (an aic_model Policy plugin), run.py (one goal, one run),
               world.py (AicWorld), skills.py (CheatCode's phases), faults.py (#60), node.py,
               convert.py, geometry.py, force.py, feedback.py, manifest.py (scene names,
               ADR 027), scoring.py (the scorer's files as a trial's referee); ROS is
               imported only in RuntimePolicy.py and ros_port.py
tools/
  sim/         the VM batch runner, outside config_hash (ADR 028): scenes.py (engine configs),
               batch.py (one engine run), report.py, applied.py (did a scene's perturbation
               take effect, ADR 026), in_container/bag_dump.py (eval container)
eval/scenarios/   *.yaml
eval/sim/         engine_template.yaml (the engine sections of our engine configs)
docs/decisions/   NNN-title.md (design decision records)
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
  heavy module-level imports make `aic_model` miss its GetState timeout. ACT reads only the
  `Observation`, no ground truth. The fork's own note (`CLAUDE.md:140`, a local image
  `my-solution:v1`) records 107.96 / ~300 (trials 39.8, 42.8, 25.4); every trial is under 75,
  which suggests no full insertion.
- Scoring (per trial, per `docs/scoring.md`; max 100): full insertion 75, wrong port -12,
  partial 38–50, proximity 0–25; off-limit contact -24; plus smoothness, duration, path length.
- **Force penalty (-12)**: the magnitude of the tared force (torque ignored) above 20 N,
  **accumulated** over the whole task for more than 1 s — not one continuous second
  (`ScoringTier2.cc:863-903`). Several short spikes add up.
- Scenario variation: engine config sets the task-board pose and per-rail translation.
  `task_board_limits` clamps **translation only** (`aic_engine.cpp:1798-1830`); rotations come
  from the scene config unclamped, so validate them ourselves.
- Ground-truth TF is for local development only. `ground_truth` defaults to false
  (`aic_gz_bringup.launch.py:728-734`) and the official eval runs with `ground_truth:=false`
  (`docker/docker-compose.yaml:9`), so the GT frames are never published there. A Zenoh access
  list also denies the model the `/scoring/**` and simulator topics
  (`docker/aic_eval/aic_zenoh_config.json5:293-321`, `docs/access_control.md`); it is off
  locally unless `AIC_ENABLE_ACL` is set. The rules allow ground truth in training, not in evaluation
  (`docs/challenge_rules.md:31`). Fine for this project; state it in the README.
- From the first sim run (#28, `docs/phase1-run28-report.md`):
  - When `insert_cable` returns False, the scorer marks the task not completed and scores
    tiers 2 and 3 as 0 even if the plug is in. The insertion truth is the
    `/scoring/insertion_event` topic in the trial's bag.
  - A seated plug's tip is within about 1 mm of its port frame, not 10 mm under it as `verify`
    first assumed: SFP height -0.98 mm, SC +0.07 mm.
    The entrances are 45.8 mm (SFP) and 13.64 mm (SC) above the port frames.
  - CheatCode's tip offset seats the SFP plug already at z_offset 0.01, i.e. during our align.
  - Real-time factor about 0.43 on a g2-standard-8 + L4: a 32 s CheatCode task takes about
    75 s of wall time. CheatCode scored 92.5 + 92.5 + 91.9 on the sample config.
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
- **Sim runs: a GCP VM**, cloned from GitHub (or from a git bundle). The plan was a Spot
  n1-standard-8 + 1x T4, but T4s were out of stock in every zone tried during #28; the runs so
  far used an on-demand g2-standard-8 + 1x L4 (`asia-northeast3-a`), whose accelerator image
  needs the NVIDIA GL libraries added (`docs/runbooks/phase1-sim-run.md`, section 2). Results and
  the SQLite store live on a separate persistent disk.
- **Spot VMs can be preempted at any time, so the eval runner must be resumable.** The unit of
  work is one (scenario, setting, seed). A completed unit is recorded in the event store and
  skipped on restart. A unit interrupted mid-run is rerun from the start, never resumed
  half-way. The runner shuts the VM down when the queue is empty (opt-in flag).

## Conventions

- Python >= 3.11 (Kilted on Ubuntu 24.04 ships 3.12). Type hints everywhere.
- Dependencies: stdlib `sqlite3` + `asyncio`, `py_trees`, `pydantic` (schemas), `typer` (CLI),
  `pyyaml` (scenario files), `pytest`, and `openai` as the optional extra `[llm]` (NVIDIA NIM
  through its OpenAI-compatible API; the key is read only from the environment variable
  `NVIDIA_API_KEY`). Ask before adding anything else.
- Format with `ruff format`, lint with `ruff check`. Tests with `pytest -q`.
- Tests never call a real LLM; use a fake planner with scripted answers.
- Seeded randomness only (`random.Random(seed)` passed in); no global RNG.

## How to work with Shaun

- Start each phase in plan mode: propose the design, list the decisions and their trade-offs,
  and wait for approval before writing code.
- For every non-trivial decision, write `docs/decisions/NNN-title.md`: context, options and
  trade-offs, then the Decision and Why for the option chosen in plan mode, with Status
  `accepted`. Shaun delegated Decision and Why to Claude on 2026-11-09: keep the marker that
  says Claude drafted them, list every new or changed decision in the PR description, and Shaun
  accepts them by merging (he may rewrite any of it).
- Flag in the PR description any decision that changes what the eval results mean (fault
  channels, the held-out split, metric definitions), so Shaun reviews it before merging.
- Prefer small, reviewable commits per step. Explain what changed and why in the summary.
- Do not build ahead of the current phase.
- The project is measured against an Agentic Robotics Engineer role: agentic infrastructure
  first (runtime, failure handling, model calls, tracing, eval); robotics only as deep as the
  integration needs.

## Status

Phase goals, exit criteria and the capability map are in `docs/roadmap.md`. Phase specs are
cut from `docs/briefs/`; open questions per phase are issues #12, #13 and #14.

- [x] Phase 0 — ROS-free runtime core + mock world + eval CLI
- [ ] Phase 1 — AIC adapter and scripted skills; the 3 sample-config tasks end to end with no
  faults, recorded and replayable (`docs/briefs/phase-1-aic-adapter.md`). The first sim run
  (#28, `docs/phase1-run28-report.md`) did not reach 3/3: two seated plugs were judged stuck,
  and the SC plug jams at the port entrance. Since #58 judges seating by height, the two SFP
  tasks pass (scorer 75 each) in the clean runs of #61 and of its rerun; the SC jam is open
  (`docs/sim-results.md`).
- [ ] Phase 2 — to be revised after #28 (the draft is PR #42, not merged): a cost-capped fault
  discovery, then a small A/B/C eval in Gazebo reconciled with the AIC score; ACT as
  `insert_act` optional
- [ ] Phase 3 — model planner (`docs/briefs/phase-3-unknown-promotion.md`). Done: UNKNOWN
  triage on the mock world and in the sim, with a live wait, Monitor preemption and traces
  (`docs/phase3-llm-results.md`, `docs/sim-results.md`); escalation; task-to-tree; UNKNOWN
  promotion, where replay checks a promoted rule on recorded inputs (not that a recovery
  works). The UNKNOWNs are scenarios we designed: mock faults, and an injected command dropout
  in the sim. Open: forking a run. A VLM check of "is the plug seated" is allowed as an
  experiment (ADR 015) but not planned: perception and VLMs are out of scope
  (`docs/roadmap.md`).
- [ ] Phase 4 — eval report, README, demo video

The re-plan after #28, and the review that shaped it: `docs/reviews/2026-11-09-plan-grill.md`.
