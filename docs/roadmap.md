# Roadmap

Where each part of the project is designed, what each phase must prove before the next starts,
and what is out of scope on purpose. Phase specs and tickets are cut from the briefs in
`docs/briefs/`; open questions per phase live in issues #12 (Phase 1), #13 (Phase 2) and
#14 (Phase 3).

## Phases

| Phase | Goal | Exit criterion | Runs on | Status |
|---|---|---|---|---|
| 0 | ROS-free runtime core, mock world, eval CLI | eval prints the A/B/C table; kill and rerun gives the same table | laptop + CI | done (#1, #15) |
| 1 | The same runtime drives the real AIC interface | the 3 sample-config tasks succeed end to end with no faults, recorded and replayable | laptop (adapter, fakes) + GCP VM (one sim run, on-demand) | sim run done (#28, `phase1-run28-report.md`): pipeline, stop path and scorer comparison work; 3/3 not met (seated plugs judged failed, SC entrance jam) |
| 2 | What recovery is worth, measured in the sim | a cost-capped fault discovery pilot, then a small A/B/C eval in Gazebo, reconciled with the AIC score; B and C may show no gain | GCP VM | re-scoped after #28; open decisions: #13 |
| 3 | The LLM planner | first: one complete LLM path (UNKNOWN → HOLD → model call with deadline → validation → bounded action or escalation) with baselines and a trace; then task-to-tree and UNKNOWN promotion | laptop (mock) first, then VM | starts on the mock world before Phase 2; brief: `briefs/phase-3-unknown-promotion.md`; open decisions: #14 |
| 4 | Report and demo | README and interview one-pager once Phase 3's path stands; eval report and demo video after | laptop | not started |

Boundary between 1 and 2: Phase 1 ends when the pipeline works end to end with **no faults**.
Fault injection, the eval in the sim and the ACT skill (optional) are Phase 2. An adapter that has never run
in the sim is not done, so Phase 1 includes one real run.

Order after #28 (2026-11-09): Phase 3's first path on the mock world, then the README and
one-pager, then Phase 1's remaining gaps, then Phase 2. Why: Shaun's interview is close, and the
agentic side is what the project must show first. The review that led to this order is in
`reviews/2026-11-09-plan-grill.md`.

## Briefs

Each phase spec is cut from its brief with `to-spec`.

| Phase | Brief | Open decisions |
|---|---|---|
| 1 | `briefs/phase-1-aic-adapter.md` | #12 |
| 2 | `briefs/phase-2-sim-eval.md` | #13 |
| 3 | `briefs/phase-3-unknown-promotion.md` | #14 |
| 4 | `briefs/phase-4-report-and-demo.md` | — |

## Where each capability is designed

The rows follow the Agentic Robotics Engineer JD this project is measured against.

| Capability | Where | Phase |
|---|---|---|
| State management | event sourcing: state is a fold over the event log (`runtime/state.py`, ADR 004) | 0 |
| Execution feedback | `skill_progress` events; mapped to `InsertCable` feedback by the adapter | 0 → 1 |
| Failure detection | Monitor, sole authority on failure classes (ADR 006) | 0; calibrated in 2 |
| Autonomous recovery | recovery table and budgets (ADR 007) | 0; measured in 2 |
| Task management | BT executor and the `Recovering` composite | 0 |
| Async communication | asyncio bridge (ADR 003); ROS `InsertCable` action in the adapter | 0 → 1 |
| Monitoring | event log, replay, Monitor evidence; a trace export of run, skill and planner spans | 0; trace in 3 |
| Version management | `config_hash` with a source fingerprint (ADR 010) | 0; extended in 1 |
| Automated testing | CI on Ubuntu and Windows, fairness and resume tests; behavioural regression gate | 0; gate in 1 |
| Skill library | skill contract, registry, `monitor_profile`; AIC skills | 0 → 1 |
| ROS 2, BT, action servers | py_trees (0); ROS 2 and the `InsertCable` action (1); skill-level action servers optional | 0 → 1 |
| Latency, sync, long-running reliability | sim time, wrench sampling, real dt (#12) | 1–2 |
| Evaluation in simulation | fault discovery pilot and a small eval on the VM | 2 |
| LLM task planning and tool use | LLM planner behind the Phase 0 protocol; tool calling through an OpenAI-compatible API (NVIDIA NIM) | 3 |
| Agent evaluation | the LLM path against `RulePlanner` and a fixed policy: success, wasted steps, invalid output, timeouts, latency, tokens, variance over repeats | 3 |
| LLM-driven program generation | LLM writes a behaviour tree; validated against schema and skill registry before it runs | 3 (after the first path) |
| Self-correction and iteration | UNKNOWN promotion: LLM proposes a rule, replay checks the rule on recorded and held-out logs | 3 (after the first path) |

## Out of scope, on purpose

- **Perception.** Ground-truth TF stands in for perception (ADR 015 sets what may read it). The
  Monitor reads only `Signals`, so a perception stack can feed it later without changing the
  core. A VLM that judges "is the plug seated" from camera images is a later Phase 3 option,
  measured against the scorer; it is not a perception stack.
- **Motion planning and MoveIt.** Skills command the AIC controller directly.
- **Real robots and Sim2Real.**
- **C++.** A later option is to port the executor to BehaviorTree.CPP.
- **Model training.** Policies are reused as they are.
