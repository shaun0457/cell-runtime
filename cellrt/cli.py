"""`cellrt run | eval | replay`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from pydantic import ValidationError

from cellrt.eval.metrics import compute_metrics
from cellrt.eval.report import render_confusion, render_table
from cellrt.eval.runner import run_eval, run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.trees import SETTINGS
from cellrt.replay import replay_lines
from cellrt.runtime.store import EventStore

DEFAULT_DB = Path("runs") / "cellrt.sqlite"
DEFAULT_SCENARIOS = Path("eval") / "scenarios" / "phase0.yaml"
MAX_SEEDS = 1000
# Validation errors shown for an invalid scenarios file (location and type only).
MAX_SHOWN_ERRORS = 5

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)

DbOption = Annotated[Path, typer.Option(help="SQLite event store")]


@app.command()
def run(
    scenario: Annotated[str, typer.Option(help="scenario id from the scenarios file")],
    setting: Annotated[str, typer.Option(help=f"one of {', '.join(SETTINGS)}")] = "A",
    seed: Annotated[int, typer.Option(min=0)] = 0,
    scenarios: Annotated[Path, typer.Option(help="scenarios YAML file")] = DEFAULT_SCENARIOS,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Run one (scenario, setting, seed) and print its run id."""
    if setting not in SETTINGS:
        _fail(f"unknown setting {setting!r}; known: {', '.join(SETTINGS)}")
    by_id = {s.id: s for s in _load(scenarios)}
    if scenario not in by_id:
        _fail(f"unknown scenario {scenario!r}; known: {', '.join(by_id)}")
    with EventStore(db) as store:
        result = run_unit(by_id[scenario], setting, seed, store)  # type: ignore[arg-type]
    typer.echo(f"outcome: {result.outcome}")
    typer.echo(result.run_id)


@app.command(name="eval")
def eval_(
    scenarios: Annotated[Path, typer.Option(help="scenarios YAML file")] = DEFAULT_SCENARIOS,
    settings: Annotated[str, typer.Option(help="comma-separated settings")] = ",".join(SETTINGS),
    seeds: Annotated[int, typer.Option(min=1, max=MAX_SEEDS, help="runs seeds 0..N-1")] = 20,
    k: Annotated[int, typer.Option(min=1, help="k for pass^k")] = 5,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Run every scenario x setting x seed, then print metrics read from the event log."""
    # In the order given, each once: a repeated setting would count its runs twice.
    chosen = list(dict.fromkeys(s.strip() for s in settings.split(",") if s.strip()))
    if not chosen:
        _fail("no settings given")
    for setting in chosen:
        if setting not in SETTINGS:
            _fail(f"unknown setting {setting!r}; known: {', '.join(SETTINGS)}")
    loaded = _load(scenarios)
    with EventStore(db) as store:
        run_ids = run_eval(loaded, chosen, seeds, store)  # type: ignore[arg-type]
        table = compute_metrics((store.events(r) for r in run_ids), k)
    for line in [*render_table(table), "", *render_confusion(table)]:
        typer.echo(line)


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
        where = [
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}"
            for e in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        more = len(where) - MAX_SHOWN_ERRORS
        shown = where[:MAX_SHOWN_ERRORS] + ([f"... and {more} more"] if more > 0 else [])
        _fail("\n  ".join([f"invalid scenarios file {path}:", *shown]))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # not UTF-8, e.g. UTF-16
        _fail(f"cannot read scenarios file {path} ({type(exc).__name__})")


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)
