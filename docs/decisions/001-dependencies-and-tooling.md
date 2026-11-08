# 001: Scenario file format and environment tooling

Date: 2026-11-08
Status: proposed

## Context

Two choices have to be made before any core code exists.

1. **Scenario file format.** The Phase 0 kickoff names `eval/scenarios/phase0.yaml`. CLAUDE.md
   allows only stdlib, py_trees, pydantic, typer and pytest as dependencies and says to ask
   before adding anything else. The scenario file holds nested data (a scenario has a list of
   faults, each with parameter ranges), and Phase 2 adds scenario generation from
   `task_board_limits`, which is nested as well.
2. **Environment and dependency tooling.** Development happens on Windows (Python 3.12 is
   available through conda and a system install), CI runs on ubuntu-latest and windows-latest,
   and Phase 2 runs on a fresh GCP Spot VM cloned from GitHub. All three should install the same
   versions.

Either way, scenario files are validated by pydantic models after parsing, so the parser only
turns text into dicts and lists.

## Options

### Format A — YAML with PyYAML (`yaml.safe_load`)
- Pros: keeps the file name from the kickoff; nested lists of faults read naturally; widely
  known; `safe_load` does not construct arbitrary objects.
- Cons: one more runtime dependency; YAML's implicit typing (`no` → `False`, `1e3` as a string)
  can surprise, though pydantic validation catches type mismatches.

### Format B — TOML with stdlib `tomllib`
- Pros: no new dependency; explicit typing; already the format of `pyproject.toml`.
- Cons: nested arrays of tables (`[[scenarios.faults]]`) are verbose and harder to read for
  lists of faults; the file name differs from the kickoff.

### Tooling A — uv with a committed `uv.lock`
- Pros: one lock file pins every transitive version for Windows, Linux and CI; uv also installs
  the interpreter, so local, CI and VM use the same Python build (`python-preference =
  "only-managed"`); fast installs on a fresh VM.
- Cons: one more tool to install on every machine (winget locally, `setup-uv` in CI, a script on
  the VM); contributors must learn `uv run` / `uv sync`.

### Tooling B — venv + pip + `pyproject.toml` only
- Pros: nothing beyond Python itself; universally understood.
- Cons: no lock file, so transitive versions drift between machines and over time; the
  interpreter is whatever each machine has (here, conda's), which weakens "same seed, same
  result" across environments.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.
