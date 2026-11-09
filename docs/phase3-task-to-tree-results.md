# Phase 3 results: a real model as the task planner (ADR 023)

What happened when each run asked a model on NVIDIA NIM for its plan before anything moved
(`--tree llm`), next to the fixed steps and the plug-type table. Run on 2026-11-10.

**Short answer: the model added nothing over the table.** With no recovery for UNKNOWN
(setting B) it seated 60% of the tasks, against 100% for the table and 50% for the fixed steps.
With the planner for UNKNOWN (setting C) every tree reached 100%, because the recovery layer
repaired the model's plans. In these answers its choice of insert skill did not follow the plug
type: it chose `insert_act` for 2 of 9 SC tasks and for 4 of 10 SFP tasks, and it gave two
different plans to the same prompt at temperature 0. Every answer it gave was a valid plan; one
call of 20 hit the 30 s deadline.

Read this with three limits in mind:

- **The scenarios are ours.** `eval/scenarios/phase3-tasks.yaml` (ADR 023) holds an SFP task
  with no fault and an SC task whose plug, in the mock, wedges rotated on every attempt, after
  #28's SC jam in the sim. The table's row (`sc` -> `insert_act`) is ours too: it encodes the
  answer by hand.
- **The model is told less than the table knows.** The prompt gives the task's fields and each
  skill's description and step contract. `insert_act`'s description says it frees a plug rotated
  against the port; nothing says an SC plug wedges. Telling it so would be telling it the answer.
- **The world is the mock, and a model is a sample.** The mock's clock does not run during the
  plan call, so the sim-time columns do not include its latency. The llm rows are one eval of 20
  units: one SC run more or less moves setting B by 10 points.

## Setup

| | |
|---|---|
| Code | `f622fc8` (branch `phase3/task-to-tree`) |
| Model | `nvidia/nemotron-3.5-lightning-30b-a3b` on NIM, via the `openai` SDK (ADR 017) |
| Task planner settings | defaults: `max_tokens` 1024, temperature 0, deadline 30 s, tool choice `auto` |
| Planner for UNKNOWN in C | the rule planner (no model call) |
| Units | 2 tasks x settings B, C x seeds 0-4 = 20 per tree |
| Model calls | 20 (one per unit), capped at 20 (`--max-model-calls 20`) |

Commands, from the repository root:

```sh
S=eval/scenarios/phase3-tasks.yaml
# NVIDIA_API_KEY in the environment; the openai SDK is the optional extra `llm`
uv run --extra llm cellrt eval --scenarios $S --settings B,C --seeds 5 --k 1 --tree llm \
  --max-model-calls 20 --db runs/task-plan/llm.sqlite
for t in table fixed; do
  uv run cellrt eval --scenarios $S --settings B,C --seeds 5 --k 1 --tree $t \
    --db runs/task-plan/$t.sqlite
done
uv run cellrt replay sfp/B/0/a1 --db runs/task-plan/llm.sqlite
```

The key came from the environment of the `--tree llm` process only; no output or store contains
it. The three stores (1.6-1.8 MB each) are kept outside the repository; `cellrt replay` reads any
run in them without the model.

## Results

| tree | setting | success | planner calls / task | mean steps | mean duration (sim s) | plan calls | plans rejected |
|---|---|---|---|---|---|---|---|
| fixed | B | 50% | 0.00 | 3.5 | 7.6 | 0 | - |
| fixed | C | 100% | 0.50 | 4.5 | 9.1 | 0 | - |
| table | B | 100% | 0.00 | 4.0 | 9.8 | 0 | 0 of 10 |
| table | C | 100% | 0.00 | 4.0 | 9.8 | 0 | 0 of 10 |
| llm | B | 60% | 0.00 | 3.3 | 8.3 | 10 | 1 of 10 |
| llm | C | 100% | 0.40 | 4.4 | 9.5 | 10 | 0 of 10 |

"Plans rejected" is what `cellrt eval` prints: the llm B row's 1 is the call cut at the 30 s
deadline, not a plan the check refused. The means include rejected and failed runs (ADR 023):
that run counts as 0 steps and 0 s.

## What the model chose

Over 19 answers there were two distinct plans, both valid:

| task | `approach > align > insert_act > verify` | `approach > align > insert_scripted > verify` | no plan |
|---|---|---|---|
| SC (wedges rotated) | 2 | 7 | 1 (deadline) |
| SFP (no fault) | 4 | 6 | 0 |

- **SC, setting B:** the one `insert_act` plan seated the plug (11.75 s); the three
  `insert_scripted` plans stalled, the Monitor called it UNKNOWN, and B has no recovery for that;
  the seed 0 run's call hit the deadline and the run ended `aborted` before any motion.
- **SC, setting C:** the four `insert_scripted` plans stalled too, and the rule planner switched
  each to `insert_act` (one planner call each); all five runs seated the plug.
- **SFP:** every plan seated the plug. The `insert_act` plans took 11.1-11.35 s against
  7.85-8.1 s for `insert_scripted`, because `insert_act` descends at half speed: about 3.2 s of
  sim time each, for nothing.
- The confusion matrix of the llm eval lists 2 SC faults as missed: in the two `insert_act` runs
  the plug was freed before any verdict (the caveat in ADR 023).

## The calls

| | |
|---|---|
| answered | 19 of 20; 1 cancelled at the 30 s deadline (`planner_timed_out`) |
| valid plans | 19 of 19 answers (no parse or check rejection) |
| latency (answered) | 3.25-7.04 s, median 4.89 s |
| prompt tokens | 866 (SC), 870 (SFP) |
| completion tokens | 252-436, mean 347 |

The recorded answer is about 55 characters (the tool call's arguments), yet each call counted
252-436 completion tokens; the rest is not in the reply we record, so what the model generated
there is not known. The same prompt gave different plans at temperature 0; why the hosted
endpoint does not repeat itself was not investigated.

## What this shows and does not show

- The plan path works with a real model: one call per run before motion, a strict schema, the
  step check, the trace, the shared cap and the deadline. Every answer passed the schema and the
  check, so on this prompt the check never had to refuse anything; its refusals are tested with
  a scripted client only.
- On these 19 answers the model's choice of insert skill did not follow the plug type
  (`insert_act` for 2 of 9 SC and 4 of 10 SFP tasks). A plan that is right by chance is worth
  less than the table, which costs no call.
- In setting C the recovery layer hides most of the difference: success is 100% for every tree.
  What the model's plans saved over the fixed steps is one planner call in ten tasks (0.40
  against 0.50 per task), at a longer mean duration (9.5 s against 9.1 s), for 10 plan calls.
- It does not show what a model would do with a prompt that says what SC plugs do, with another
  model, or in the sim. One eval of 20 units; the plan mix (2 of 9, 4 of 10) is a small sample.
