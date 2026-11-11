# Roadmap

Where each part of the project is designed, what each phase must prove before the next starts,
and what is out of scope on purpose. Phase specs and tickets are cut from the briefs in
`docs/briefs/`; open questions per phase live in issues #12 (Phase 1), #13 (Phase 2) and
#14 (Phase 3).

## Phases

| Phase | Goal | Exit criterion | Runs on | Status |
|---|---|---|---|---|
| 0 | ROS-free runtime core, mock world, eval CLI | eval prints the A/B/C table; kill and rerun gives the same table | laptop + CI | done (#1, #15) |
| 1 | The same runtime drives the real AIC interface | the 3 sample-config tasks succeed end to end with no faults in a recorded, replayable run, and the rate over three clean runs is recorded (revised 2026-11-11, below) | laptop (adapter, fakes) + GCP VM (on-demand) | done (#73): 2 of the 3 clean runs with the merged hold (`0014a07`) seated all three plugs; SFP 6/6, SC 2/3. `cellrt replay` was not run on these sessions (`sim-results.md`) |
| 2 | What recovery is worth, measured in the sim | A/B/C eval in Gazebo with injected faults, reconciled with the AIC score; B and C may show no gain | Spot VM | not started as a phase; draft in PR #42 (not merged), open decisions: #13. An injected dropout already ran A, B and C in Gazebo once, for Phase 3's triage (#60, #61) |
| 3 | The LLM planner | LLM triage and task-to-tree generation pass the same validation gate; UNKNOWN promotion validated by replay | laptop + on-demand VM | mostly done; forking a run is open. Done: triage on the mock and in the sim, escalation, task-to-tree, UNKNOWN promotion (`phase3-*-results.md`, `sim-results.md`); open decisions: #14 |
| 4 | Report and demo | eval report, README, demo video | laptop | in progress: demo video composed (kept outside the repository); README next |

Phase 1's criterion was revised when it closed. It first read "the 3 sample-config tasks succeed
end to end with no faults in a recorded, replayable run", which can be read two ways:
- **One run passes all three.** Two of the three clean runs with the merged entry hold
  (ADR 024, `0014a07`) did, so this reading is met.
- **Every clean run passes all three.** ADR 024's time box used this reading, and it is not
  met: in the third run the SC plug stuck on the entrance rim, after a stop that ended above
  the entrance.

The revision keeps the first reading and adds the rate over three clean runs, with no
threshold. It is not a higher bar than the first reading. What it adds is that the result
shows how often the cell passes as well as that it can: SFP 6 of 6, SC 2 of 3. The SC rim jam
is a known limit. An earlier clean run with the hold's first version (`439e58b`) went 2/3 and
is not in the rate (`sim-results.md`). Read the criterion the second way and Phase 1 is not
done.

Boundary between 1 and 2: Phase 1 ends when the pipeline works end to end with **no faults**.
Fault injection, the eval in the sim and the ACT skill are Phase 2. The injected dropout was
built early, for Phase 3's triage in the sim (#60, #61). Phase 2's measured eval is not built.
An adapter that has never run in the sim is not done, so Phase 1 includes one real run.

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
| Monitoring | event log, replay, Monitor evidence; traces of a run (`cellrt trace`) | 0; traces in 3 |
| Version management | `config_hash` with a source fingerprint (ADR 010) | 0; extended in 1 |
| Automated testing | CI on Ubuntu and Windows, fairness and resume tests; behavioural regression gate | 0; gate in 1 |
| Skill library | skill contract, registry, `monitor_profile`; AIC skills | 0 → 1 |
| ROS 2, BT, action servers | py_trees (0); ROS 2 and the `InsertCable` action (1); skill-level action servers optional | 0 → 1 |
| Latency, sync, long-running reliability | sim time, wrench sampling, real dt (#12) | 1–2 |
| Evaluation in simulation | eval harness on the Spot VM | 2 |
| LLM task planning and tool use | LLM planner behind the Phase 0 protocol | 3 |
| LLM-driven program generation | LLM writes a behaviour tree; validated against schema and skill registry before it runs | 3 |
| Self-correction and iteration | UNKNOWN promotion: LLM proposes a rule, replay validates it | 3 |

## Out of scope, on purpose

- **Perception and VLMs.** Ground-truth TF stands in for perception. The Monitor reads only
  `Signals`, so a perception stack can feed it later without changing the core.
- **Motion planning and MoveIt.** Skills command the AIC controller directly.
- **Real robots and Sim2Real.**
- **C++.** A later option is to port the executor to BehaviorTree.CPP.
- **Model training.** Policies are reused as they are.
