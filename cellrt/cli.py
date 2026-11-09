"""`cellrt run | eval | replay`."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from pydantic import ValidationError

from cellrt.eval.baseline import MAX_BASELINE_SEEDS, BaselineConfig, BaselineFile, compare, snapshot
from cellrt.eval.metrics import compute_metrics
from cellrt.eval.report import NOTES, render_confusion, render_gains, render_table
from cellrt.eval.runner import MAX_ATTEMPTS, run_eval, run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.trees import SETTINGS
from cellrt.planner.base import Planner
from cellrt.planner.fixed_planner import FixedPlanner
from cellrt.planner.rule_planner import RulePlanner
from cellrt.replay import replay_lines
from cellrt.runtime.store import EventStore

DEFAULT_DB = Path("runs") / "cellrt.sqlite"
DEFAULT_SCENARIOS = Path("eval") / "scenarios" / "phase0.yaml"
MAX_SEEDS = 1000
# `cellrt baseline --write` defaults: the config of eval/baselines/phase0-small.json.
BASELINE_SEEDS = 3
BASELINE_K = 2
# Validation errors shown for an invalid scenarios file (location and type only).
MAX_SHOWN_ERRORS = 5
# The planners setting C can ask, by the name `--planner` takes. Each entry builds one planner
# that serves every run of the command (ADR 019).
PLANNERS: dict[str, Callable[[], Planner]] = {"rule": RulePlanner, "fixed": FixedPlanner}

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)

DbOption = Annotated[Path, typer.Option(help="SQLite event store")]
PlannerOption = Annotated[
    str, typer.Option(help=f"planner for setting C: one of {', '.join(PLANNERS)}")
]


@app.command()
def run(
    scenario: Annotated[str, typer.Option(help="scenario id from the scenarios file")],
    setting: Annotated[str, typer.Option(help=f"one of {', '.join(SETTINGS)}")] = "A",
    seed: Annotated[int, typer.Option(min=0)] = 0,
    scenarios: Annotated[Path, typer.Option(help="scenarios YAML file")] = DEFAULT_SCENARIOS,
    db: DbOption = DEFAULT_DB,
    planner: PlannerOption = "rule",
) -> None:
    """Run one (scenario, setting, seed) and print its run id."""
    if setting not in SETTINGS:
        _fail(f"unknown setting {setting!r}; known: {', '.join(SETTINGS)}")
    chosen = _planner(planner)
    by_id = {s.id: s for s in _load(scenarios)}
    if scenario not in by_id:
        _fail(f"unknown scenario {scenario!r}; known: {', '.join(by_id)}")
    with EventStore(db) as store:
        result = run_unit(
            by_id[scenario],
            setting,  # type: ignore[arg-type]
            seed,
            store,
            planner=chosen if setting == "C" else None,
        )
    typer.echo(f"outcome: {result.outcome}")
    typer.echo(result.run_id)


@app.command(name="eval")
def eval_(
    scenarios: Annotated[Path, typer.Option(help="scenarios YAML file")] = DEFAULT_SCENARIOS,
    settings: Annotated[str, typer.Option(help="comma-separated settings")] = ",".join(SETTINGS),
    seeds: Annotated[int, typer.Option(min=1, max=MAX_SEEDS, help="runs seeds 0..N-1")] = 20,
    k: Annotated[int, typer.Option(min=1, help="k for pass^k")] = 5,
    db: DbOption = DEFAULT_DB,
    planner: PlannerOption = "rule",
) -> None:
    """Run every scenario x setting x seed, then print metrics read from the event log."""
    chosen = _settings(settings)
    chosen_planner = _planner(planner)
    loaded = _load(scenarios)
    with EventStore(db) as store:
        runs = run_eval(loaded, chosen, seeds, store, planner=chosen_planner)  # type: ignore[arg-type]
        table = compute_metrics((store.events(r) for r in runs.run_ids), k)
    # On stderr, so the table on stdout is the same whether or not the eval was resumed.
    typer.echo(f"ran {runs.ran} units, {runs.skipped} already finished", err=True)
    if "C" in chosen:  # the table's C row does not say which planner it ran with
        typer.echo(f"planner for setting C: {planner}", err=True)
    if runs.gave_up:
        typer.echo(
            f"gave up on {len(runs.gave_up)} units interrupted {MAX_ATTEMPTS} times"
            f" (not in the table): {', '.join(runs.gave_up)}",
            err=True,
        )
    gains = render_gains(table)
    for line in [
        *render_table(table), "", *render_confusion(table), "",
        *gains, *([""] if gains else []), *NOTES,
    ]:  # fmt: skip
        typer.echo(line)


@app.command()
def baseline(
    path: Annotated[Path, typer.Argument(help="baseline JSON file")],
    write: Annotated[bool, typer.Option(help="run the eval below and write the file")] = False,
    scenarios: Annotated[Path, typer.Option(help="scenarios YAML file")] = DEFAULT_SCENARIOS,
    settings: Annotated[str, typer.Option(help="comma-separated settings")] = ",".join(SETTINGS),
    seeds: Annotated[
        int, typer.Option(min=1, max=MAX_BASELINE_SEEDS, help="runs seeds 0..N-1")
    ] = BASELINE_SEEDS,
    k: Annotated[int, typer.Option(min=1, help="k for pass^k")] = BASELINE_K,
) -> None:
    """Check the eval a baseline file names against the table it records; with --write, run the
    eval given by the options and record it. The eval runs in a fresh store every time."""
    if write:
        if scenarios.is_absolute():
            _fail("--scenarios must be relative to the repository root, so CI can find it")
        config = BaselineConfig(
            scenarios=scenarios.as_posix(), settings=_settings(settings), seeds=seeds, k=k
        )
        recorded = {"config": config.model_dump(), "table": _baseline_table(config)}
        try:
            text = json.dumps(recorded, indent=2, sort_keys=True, allow_nan=False) + "\n"
        except ValueError:
            _fail(f"the eval gave a non-finite number; {path} not written")
        _write_whole(path, text)
        typer.echo(f"wrote {path}")
        return
    try:
        baseline_file = BaselineFile.model_validate_json(path.read_bytes())
    except OSError as exc:
        _fail(f"cannot read baseline file {path} ({type(exc).__name__})")
    except ValidationError as exc:
        _fail("\n  ".join([f"invalid baseline file {path}:", *_where(exc)]))
    differences = compare(baseline_file.table, _baseline_table(baseline_file.config))
    if differences:
        _fail("\n  ".join([f"the eval no longer matches {path}:", *differences]))
    typer.echo(f"matches {path}")


def _write_whole(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file beside it, so an interrupted or failed
    write leaves the old file whole. A failure names the error type only."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        _fail(f"cannot write baseline file {path} ({type(exc).__name__})")


def _baseline_table(config: BaselineConfig) -> dict[str, object]:
    """Run the eval `config` names in a fresh store and snapshot its table. Paths are relative
    to the current directory: run from the repository root."""
    chosen = _settings(",".join(config.settings))
    loaded = _load(Path(config.scenarios))
    with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "baseline.sqlite") as store:
        runs = run_eval(loaded, chosen, config.seeds, store)  # type: ignore[arg-type]
        table = compute_metrics((store.events(r) for r in runs.run_ids), config.k)
    return snapshot(table)


def _settings(settings: str) -> list[str]:
    """The settings named, in the order given, each once; exit on an unknown one."""
    # A repeated setting would count its runs twice.
    chosen = list(dict.fromkeys(s.strip() for s in settings.split(",") if s.strip()))
    if not chosen:
        _fail("no settings given")
    for setting in chosen:
        if setting not in SETTINGS:
            _fail(f"unknown setting {setting!r}; known: {', '.join(SETTINGS)}")
    return chosen


def _planner(name: str) -> Planner:
    """The planner `--planner` names (used by setting C only); exit on an unknown name."""
    if name not in PLANNERS:
        _fail(f"unknown planner {name!r}; known: {', '.join(PLANNERS)}")
    return PLANNERS[name]()


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="run id printed by `cellrt run`")],
    db: DbOption = DEFAULT_DB,
    signals: Annotated[bool, typer.Option(help="include every signal sample")] = False,
) -> None:
    """Print every step of a run from its event log."""
    if not db.exists():
        _fail(f"no events for run {run_id} (no event store at {db})")
    with EventStore(db) as store:
        events = store.events(run_id)
    if not events:
        _fail(f"no events for run {run_id}")
    for line in replay_lines(events, signals=signals):
        typer.echo(line)


def _load(path: Path) -> list[Scenario]:
    """The scenarios in `path`, or exit with where the file is wrong (locations and error types,
    never the values in it)."""
    try:
        return load_scenarios(path)
    except ValidationError as exc:
        _fail("\n  ".join([f"invalid scenarios file {path}:", *_where(exc)]))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # not UTF-8, e.g. UTF-16
        _fail(f"cannot read scenarios file {path} ({type(exc).__name__})")


def _where(exc: ValidationError) -> list[str]:
    """Where a file is invalid: locations and error types, never the values in it."""
    where = [
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}"
        for e in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
    more = len(where) - MAX_SHOWN_ERRORS
    return where[:MAX_SHOWN_ERRORS] + ([f"... and {more} more"] if more > 0 else [])


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)
