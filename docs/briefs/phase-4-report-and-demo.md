# Phase 4 brief: the report, the README and the demo

Input for `to-spec`. Phase 4 turns the project into something a hiring manager can judge in
five minutes and an engineer can check in an hour. It adds no runtime features.

## Problem

The work is spread over 14+ ADRs, three results documents, a runbook and hundreds of tests. A
reader who opens the repo cold cannot tell what was built, what was measured or how much to
trust it. In an interview Shaun has minutes, not an hour, and the first question will be about
the numbers.

## Audiences

1. **Hiring manager or PM (5 minutes):** what problem, what result, what Shaun decided.
2. **Engineer reviewing the repo (1 hour):** can I run it, are the numbers honest, is the design
   sound.
3. **Shaun in an interview:** the order to tell it in, and the weaknesses to name first.

**Revised after #28 (2026-11-09).** Shaun's interview is close, so the README and the interview
one-pager are drafted as soon as Phase 3's first LLM path stands. They are revised when Phase 2
results exist; they do not wait for them.

## Prerequisites

- **Verifiable evidence of a real model in the loop.** Phase 3's first LLM path has a results
  document that names the command and the store behind each number. A result showing no gain
  over the simple policy counts.
- Phase 2 results if they exist. Otherwise the README states what the sim run (#28) showed and
  what it did not, and where the work stopped.
- **Every ADR is accepted, and Shaun has read each one.** Decision and Why are drafted by
  Claude under his delegation; the report quotes the ADRs and an interviewer may open any of
  them, so he must be able to explain each in his own words (the interview cards help).

## Deliverables

1. **README** (the front page):
   - one paragraph: the problem and the result, with the headline numbers and their caveat;
   - one architecture diagram (the layers, the Monitor, the event log, the eval harness);
   - "run it in one command": the mock eval with `uv`, no ROS needed, finishes in under a
     minute;
   - "what is real and what is mocked": ground-truth TF instead of perception, which faults are
     scene faults and which are injected, and that ground truth is blocked in the AIC
     submission environment;
   - links to the eval report, the ADR index and the runbook.
2. **Eval report** (`docs/eval-report.md`): method (settings, fault channels, the held-out
   split, the noise floor, the budget), results with intervals, the per-scenario breakdown, the
   reconciliation with the AIC scorer, negative results, and threats to validity. Merges what is
   still true from `docs/phase0-results.md` and the Phase 2 results.
3. **ADR index** (`docs/decisions/README.md`): one line per ADR, its decision in plain words,
   accepted or superseded.
4. **Demo video**, 3–4 minutes:
   - one clean sim run (recorded on the VM);
   - one run with a fault: the Monitor verdict, the recovery or the planner's decision, and the
     outcome (a mock run is acceptable if no sim run shows one);
   - `cellrt replay` of that run, showing the force curve and each decision, including the
     model's prompt and answer;
   - the A/B/C table, with one sentence on what it does not show.

   Screen capture of Gazebo and the terminal, with captions; no voice-over required.
5. **Interview one-pager** (kept outside the repo or in `docs/`). It holds:
   - the 30-second version;
   - the three decisions Shaun is proudest of;
   - the three weaknesses he names first;
   - questions and answers drawn from the attacks in `docs/reviews/2026-11-09-plan-grill.md`;
   - the answer to "what did you build and what did Claude Code build".

## Decisions (plan mode)

1. **Repository visibility.** The repo is private today. Before it goes public: check history
   for secrets and personal paths, pick a licence compatible with the AIC fork (Apache 2.0), and
   decide whether internal docs (kickoff prompts, briefs) stay in the public tree.
2. **Headline numbers.** Which single table leads the README, and which caveat travels with it
   in the same paragraph. Mock and sim numbers are never shown in one table without labels.
3. **Where the video lives** (repo release asset, YouTube unlisted, or a link in the README) and
   how it is reproduced (the exact scenario, setting and seed are named, so anyone can rerun it).

## Exit criteria

1. A reader with only the README can run the mock eval and get the published Phase 0 table.
2. Every number in the README and the eval report links to the command or the store that
   produced it.
3. The ADR index lists every ADR as accepted or superseded, each with its decision in one line.
4. The video shows a fault, a verdict, a recovery and a replay, and names the unit it shows.
5. Shaun has given the 30-second version and answered five follow-up questions from the
   one-pager without notes.

## Out of scope

- New runtime features, new scenarios, or rerunning evals to improve numbers.
- Polishing code that is not on the reader's path.
