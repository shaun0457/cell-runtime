"""`cellrt run | replay` (eval arrives with its ticket)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from pydantic import ValidationError

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.executor.trees import SETTINGS
from cellrt.replay import replay_lines
from cellrt.runtime.store import EventStore

DEFAULT_DB = Path("runs") / "cellrt.sqlite"
DEFAULT_SCENARIOS = Path("eval") / "scenarios" / "phase0.yaml"

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
    try:
        by_id = {s.id: s for s in load_scenarios(scenarios)}
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        _fail(f"cannot read scenarios file {scenarios} ({type(exc).__name__})")
    if scenario not in by_id:
        _fail(f"unknown scenario {scenario!r}; known: {', '.join(by_id)}")
    with EventStore(db) as store:
        result = run_unit(by_id[scenario], setting, seed, store)  # type: ignore[arg-type]
    typer.echo(f"outcome: {result.outcome}")
    typer.echo(result.run_id)


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


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)
