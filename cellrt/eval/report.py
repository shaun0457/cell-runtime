"""Plain-text rendering of a `MetricsTable`: the per-setting table and the confusion matrix."""

from __future__ import annotations

from cellrt.eval.metrics import MISSED, NO_FAULT, MetricsTable

UNDEFINED = "-"  # ASCII: some consoles cannot print an em dash


def render_table(table: MetricsTable) -> list[str]:
    """One row per setting. A rate with nothing to measure (no verdicts, fewer runs than k) is
    shown as "-", never as 0."""
    header = [
        "setting", "runs", "success", f"pass^{table.k}", "recovery", "unsafe", ">20N_s",
        "steps", "duration_s", "planner/task", "accuracy",
    ]  # fmt: skip
    rows = [
        [
            r.setting,
            str(r.runs),
            _pct(r.success_rate),
            _pct(r.pass_k),
            _pct(r.recovery_rate),
            str(r.unsafe_runs),
            f"{r.mean_over_limit_s:.2f}",
            f"{r.mean_steps:.1f}",
            f"{r.mean_duration_s:.1f}",
            f"{r.planner_calls_per_task:.2f}",
            _pct(r.classification_accuracy),
        ]
        for r in table.rows
    ]
    return _columns([header, *rows])


def render_confusion(table: MetricsTable) -> list[str]:
    """Rows: the true fault (or "none" for a false alarm); columns: the Monitor's verdict, plus
    "missed" for a fault no verdict was attributed to."""
    truths = sorted({t for t, _ in table.confusion} - {NO_FAULT})
    predicted = sorted({p for _, p in table.confusion} - {MISSED})
    if NO_FAULT in {t for t, _ in table.confusion}:
        truths.insert(0, NO_FAULT)
    columns = [*predicted, MISSED]
    header = ["truth \\ verdict", *columns]
    rows = [
        [truth, *(str(table.confusion.get((truth, p), 0)) for p in columns)] for truth in truths
    ]
    return _columns([header, *rows])


def _pct(value: float | None) -> str:
    return UNDEFINED if value is None else f"{100 * value:.1f}%"


def _columns(cells: list[list[str]]) -> list[str]:
    """Left-align the first column, right-align the rest."""
    widths = [max(len(row[i]) for row in cells) for i in range(len(cells[0]))]
    return [
        "  ".join(
            cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i]) for i, cell in enumerate(row)
        ).rstrip()
        for row in cells
    ]
