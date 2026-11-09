"""`cellrt run | eval | baseline | replay | trace | smoke-llm | agent-report`."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from pydantic import ValidationError

from cellrt.eval.agent_report import StoreRefused, parse_label, render_agent_report, variant_rows
from cellrt.eval.baseline import MAX_BASELINE_SEEDS, BaselineConfig, BaselineFile, compare, snapshot
from cellrt.eval.metrics import compute_metrics
from cellrt.eval.report import NOTES, render_confusion, render_gains, render_table
from cellrt.eval.runner import MAX_ATTEMPTS, run_eval, run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.recovery import RecoveryConfig
from cellrt.executor.trees import SETTINGS
from cellrt.monitor.promoted import MAX_CANDIDATE_CHARS, Candidate, CandidateRefused
from cellrt.planner.base import Planner
from cellrt.planner.factory import (
    DEFAULT_MAX_MODEL_CALLS,
    PLANNER_KINDS,
    build_planner,
    check_call_budget,
    llm_client_and_config,
    model_call_cap,
)
from cellrt.planner.nim import (
    DEFAULT_MODEL,
    KEY_ENV,
    MAX_MODEL_CALLS_LIMIT,
    LlmSetupError,
    ModelCallCap,
)
from cellrt.planner.smoke import run_smoke
from cellrt.promotion.groups import (
    DEFAULT_MIN_SIZE,
    MAX_MIN_SIZE,
    SurveyTooLarge,
    group_unknowns,
    render_survey,
    survey,
)
from cellrt.promotion.replay import ReplayConfig
from cellrt.promotion.validate import GroupNotFound, check_registry, validate
from cellrt.replay import replay_lines
from cellrt.runtime.events import Event
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import mock_registry
from cellrt.trace_export import export_trace

DEFAULT_DB = Path("runs") / "cellrt.sqlite"
DEFAULT_SCENARIOS = Path("eval") / "scenarios" / "phase0.yaml"
MAX_SEEDS = 1000
# `cellrt baseline --write` defaults: the config of eval/baselines/phase0-small.json.
BASELINE_SEEDS = 3
BASELINE_K = 2
# Validation errors shown for an invalid scenarios file (location and type only).
MAX_SHOWN_ERRORS = 5

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
promote_app = typer.Typer(
    no_args_is_help=True,
    help="UNKNOWN promotion: group recurring UNKNOWNs, check a candidate rule by replay"
    " (ADR 021, ADR 022).",
)
app.add_typer(promote_app, name="promote")

DbOption = Annotated[Path, typer.Option(help="SQLite event store")]
# The planners setting C can ask, by the name `--planner` takes (`build_planner`, ADR 019). One
# planner serves every run of the command.
PlannerOption = Annotated[
    str, typer.Option(help=f"planner for setting C: one of {', '.join(PLANNER_KINDS)}")
]
ModelOption = Annotated[
    str, typer.Option(help=f"model id on NVIDIA NIM, for --planner llm (key in {KEY_ENV})")
]
MaxModelCallsOption = Annotated[
    int,
    typer.Option(
        min=0,
        max=MAX_MODEL_CALLS_LIMIT,
        help="most model calls this command may make; it does not start if it could make more",
    ),
]


@app.command()
def run(
    scenario: Annotated[str, typer.Option(help="scenario id from the scenarios file")],
    setting: Annotated[str, typer.Option(help=f"one of {', '.join(SETTINGS)}")] = "A",
    seed: Annotated[int, typer.Option(min=0)] = 0,
    scenarios: Annotated[Path, typer.Option(help="scenarios YAML file")] = DEFAULT_SCENARIOS,
    db: DbOption = DEFAULT_DB,
    planner: PlannerOption = "rule",
    model: ModelOption = DEFAULT_MODEL,
    max_model_calls: MaxModelCallsOption = DEFAULT_MAX_MODEL_CALLS,
) -> None:
    """Run one (scenario, setting, seed) and print its run id."""
    if setting not in SETTINGS:
        _fail(f"unknown setting {setting!r}; known: {', '.join(SETTINGS)}")
    by_id = {s.id: s for s in _load(scenarios)}
    if scenario not in by_id:
        _fail(f"unknown scenario {scenario!r}; known: {', '.join(by_id)}")
    cap = _cap(max_model_calls)
    recovery = RecoveryConfig()  # one config for the call pre-check and the run
    units_with_planner = int(setting == "C")
    chosen = _planner(planner, units_with_planner, recovery, model=model, cap=cap)
    with EventStore(db) as store:
        result = run_unit(
            by_id[scenario],
            setting,  # type: ignore[arg-type]
            seed,
            store,
            recovery=recovery,
            planner=chosen,
        )
    if planner == "llm" and chosen is not None:
        typer.echo(f"model calls: {cap.used} of {cap.max_calls}", err=True)
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
    model: ModelOption = DEFAULT_MODEL,
    max_model_calls: MaxModelCallsOption = DEFAULT_MAX_MODEL_CALLS,
) -> None:
    """Run every scenario x setting x seed, then print metrics read from the event log."""
    chosen = _settings(settings)
    loaded = _load(scenarios)
    cap = _cap(max_model_calls)
    recovery = RecoveryConfig()  # one config for the call pre-check and the eval
    # Every unit of setting C may ask the planner, finished ones included: an upper bound.
    units_with_planner = len(loaded) * seeds if "C" in chosen else 0
    chosen_planner = _planner(planner, units_with_planner, recovery, model=model, cap=cap)
    with EventStore(db) as store:
        runs = run_eval(
            loaded,
            chosen,  # type: ignore[arg-type]
            seeds,
            store,
            recovery=recovery,
            planner=chosen_planner,
        )
        table = compute_metrics((store.events(r) for r in runs.run_ids), k)
    # On stderr, so the table on stdout is the same whether or not the eval was resumed.
    typer.echo(f"ran {runs.ran} units, {runs.skipped} already finished", err=True)
    if "C" in chosen:  # the table's C row does not say which planner it ran with
        typer.echo(f"planner for setting C: {_planner_name(planner, model)}", err=True)
    if planner == "llm" and chosen_planner is not None:
        typer.echo(f"model calls: {cap.used} of {cap.max_calls}", err=True)
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
        _write_whole(path, text, what="baseline file")
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


def _write_whole(path: Path, text: str, *, what: str) -> None:
    """Write `text` to `path` through a temporary file beside it, so an interrupted or failed
    write leaves the old file whole. A failure names `what` and the error type only."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        _fail(f"cannot write {what} {path} ({type(exc).__name__})")


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


def _planner(
    name: str,
    units_with_planner: int,
    recovery: RecoveryConfig,
    *,
    model: str,
    cap: ModelCallCap,
) -> Planner | None:
    """The planner `--planner` names, for the units of setting C; None when there are none, so
    a command without setting C needs no key. A model planner is built only once the most
    model calls those units can make under `recovery` fit `cap`. Exits with a fixed message
    on an unknown name or a refused setting; the key is in no message."""
    if name not in PLANNER_KINDS:
        _fail(f"unknown planner {name!r}; known: {', '.join(PLANNER_KINDS)}")
    if units_with_planner == 0:
        return None
    try:
        if name == "llm":
            check_call_budget(units_with_planner, recovery.planner_budget, cap.max_calls)
        return build_planner(name, model=model, cap=cap)
    except LlmSetupError as exc:
        _fail(str(exc))


def _planner_name(name: str, model: str) -> str:
    return f"{name} ({model})" if name == "llm" else name


def _cap(max_model_calls: int) -> ModelCallCap:
    """One cap for every model call the command makes."""
    try:
        return model_call_cap(max_model_calls)
    except LlmSetupError as exc:
        _fail(str(exc))


@app.command(name="smoke-llm")
def smoke_llm(
    model: ModelOption = DEFAULT_MODEL,
    mode: Annotated[str, typer.Option(help="how the model answers: tool or json")] = "tool",
) -> None:
    """Send one fixed failure (the yaw mismatch) to the model planner, once, and print how the
    model answered as JSON: through the tool or as text, valid or not, tokens and latency.
    Exits 1 unless the answer passed the planner's validation."""
    try:
        client, config = llm_client_and_config(
            model=model,
            mode=mode,  # type: ignore[arg-type]  # checked by the config
            cap=model_call_cap(1),
        )
    except LlmSetupError as exc:
        _fail(str(exc))
    report = run_smoke(client, config)
    typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    if not report.valid:
        raise typer.Exit(code=1)


@app.command(name="agent-report")
def agent_report(
    db: Annotated[
        list[str],
        typer.Option(
            help="LABEL=PATH, once per store: one variant per store; name#N marks repeats"
        ),
    ],
) -> None:
    """Print the planner variants side by side (#51): success and its spread over repeats, what
    a failed run wastes after its first UNKNOWN, planner calls and decisions, invalid answers,
    timeouts, latency and tokens."""
    stores: list[tuple[str, Path]] = []
    seen: dict[Path, str] = {}
    for arg in db:
        label, sep, path = arg.partition("=")
        if not sep or not path:
            _fail("--db takes LABEL=PATH, for example C-llm#1=runs/llm1.sqlite")
        try:
            parse_label(label)
        except StoreRefused as exc:
            _fail(f"--db takes LABEL=PATH: {exc}")
        if not Path(path).is_file():  # never create a store that is not there
            _fail(f"{label}: no event store at {path}")
        resolved = Path(path).resolve()
        if resolved in seen:  # one store given as two repeats would fake a stable spread
            _fail(f"{label}: the same event store as {seen[resolved]} ({path})")
        seen[resolved] = label
        stores.append((label, Path(path)))
    try:
        rows = variant_rows([(label, _runs_of(label, path)) for label, path in stores])
    except StoreRefused as exc:
        _fail(str(exc))
    for line in render_agent_report(rows):
        typer.echo(line)


@promote_app.command(name="group")
def promote_group(
    db: DbOption = DEFAULT_DB,
    min_size: Annotated[
        int,
        typer.Option(min=1, max=MAX_MIN_SIZE, help="verdicts a group needs to be a candidate"),
    ] = DEFAULT_MIN_SIZE,
) -> None:
    """Replay the Monitor over every run in a mock store, check that it reproduces each
    recorded verdict (gate 0, ADR 022), and print the UNKNOWN verdicts grouped by signature."""
    if not db.is_file():  # never create a store that is not there
        _fail(f"no event store at {db}")
    try:
        runs = survey(_runs_of("store", db), ReplayConfig.mock())
    except (StoreRefused, SurveyTooLarge) as exc:
        _fail(str(exc))
    lines, passed = render_survey(runs, group_unknowns(runs), min_size)
    for line in lines:
        typer.echo(line)
    if not passed:
        raise typer.Exit(code=1)


@promote_app.command(name="validate")
def promote_validate(
    candidate: Annotated[Path, typer.Argument(help="candidate JSON: a rule and its plan")],
    group: Annotated[
        str, typer.Option(help="the group's label, as `cellrt promote group` prints it")
    ],
    db: DbOption = DEFAULT_DB,
    out: Annotated[Path | None, typer.Option(help="write the report as JSON to this file")] = None,
    min_size: Annotated[
        int,
        typer.Option(min=1, max=MAX_MIN_SIZE, help="windows the group needs (gate 1)"),
    ] = DEFAULT_MIN_SIZE,
) -> None:
    """Check a candidate rule by replay against a mock store (gates 0-3, ADR 022). Replay checks
    the rule, not its plan; the report says so. Exits 1 unless every gate passes."""
    if not db.is_file():  # never create a store that is not there
        _fail(f"no event store at {db}")
    try:
        with candidate.open(encoding="utf-8") as f:
            raw = f.read(MAX_CANDIDATE_CHARS + 1)  # the parser refuses anything longer
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"cannot read the candidate at {candidate} ({type(exc).__name__})")
    try:
        parsed = Candidate.parse(raw)
        check_registry(parsed, mock_registry())
        report = validate(
            _runs_of("store", db), parsed, group, ReplayConfig.mock(), min_size=min_size
        )
    except (CandidateRefused, GroupNotFound, StoreRefused, SurveyTooLarge) as exc:
        _fail(str(exc))
    for line in report.lines():
        typer.echo(line)
    if out is not None:
        try:
            out.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            _fail(f"cannot write the report at {out} ({type(exc).__name__})")
    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="run id printed by `cellrt run`")],
    db: DbOption = DEFAULT_DB,
    signals: Annotated[bool, typer.Option(help="include every signal sample")] = False,
) -> None:
    """Print every step of a run from its event log."""
    for line in replay_lines(_run_events(run_id, db), signals=signals):
        typer.echo(line)


def _runs_of(label: str, path: Path) -> Iterator[list[Event]]:
    """Every run's events in the store at `path`, read one run at a time; the store is open only
    while its runs are read. A store that cannot be read is refused by the error's type, never
    its text, which can quote the database."""
    try:
        with EventStore(path) as store:
            for run in store.runs():
                yield store.events(run.run_id)
    except (sqlite3.Error, OSError, ValueError) as exc:  # pydantic's ValidationError included
        raise StoreRefused(
            f"{label}: cannot read the event store at {path} ({type(exc).__name__})"
        ) from None


class TraceClockOption(StrEnum):
    wall = "wall"
    sim = "sim"


@app.command()
def trace(
    run_id: Annotated[str, typer.Argument(help="run id printed by `cellrt run`")],
    db: DbOption = DEFAULT_DB,
    out: Annotated[Path | None, typer.Option(help="file to write; stdout when absent")] = None,
    clock: Annotated[
        TraceClockOption,
        typer.Option(
            help="wall: when each event was recorded, so a model call shows its real cost;"
            " sim: the run's clock, as the robot saw it"
        ),
    ] = TraceClockOption.wall,
) -> None:
    """Export a run as Chrome trace event JSON, for https://ui.perfetto.dev or chrome://tracing."""
    events = _run_events(run_id, db)
    # Replacing the store would lose the log for good: it is append-only only through SQLite.
    if out is not None and out.exists() and os.path.samefile(out, db):
        _fail("--out is the event store itself; no trace written")
    try:
        text = json.dumps(export_trace(events, clock=clock.value), allow_nan=False)
    except ValueError:
        _fail(f"run {run_id} has a time that is not a finite number; no trace written")
    if out is None:
        typer.echo(text)
        return
    _write_whole(out, text + "\n", what="trace file")
    typer.echo(f"wrote {out}")


def _run_events(run_id: str, db: Path) -> list[Event]:
    """Every event of `run_id`, or exit with a fixed message. A missing store is not created."""
    if not db.exists():
        _fail(f"no events for run {run_id} (no event store at {db})")
    with EventStore(db) as store:
        events = store.events(run_id)
    if not events:
        _fail(f"no events for run {run_id}")
    return events


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
