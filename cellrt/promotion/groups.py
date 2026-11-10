"""Group the UNKNOWN verdicts of replayed runs by a signature of their evidence (spec step 1).

The signature is the skill, the rule, and the band the force and the torque fell in. The band
edges come from thresholds the cell already has, not from the data: the Monitor's stuck force
and the force limit, and the torque `insert_act` reacts to. Grouping reads only the recorded
evidence; the injected fault kinds of each group's runs are kept beside it as referee labels,
for judging how well a signature separates failures (ADR 015), never for grouping.
"""

from __future__ import annotations

import bisect
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NamedTuple

from cellrt.monitor.classifier import FailureClass, MonitorConfig
from cellrt.promotion.replay import Judged, ReplayConfig, RunReplay, replay_run
from cellrt.runtime.events import Event, FaultInjected
from cellrt.runtime.state import FORCE_LIMIT_N
from cellrt.skills.mock import InsertAct

DEFAULT_FORCE_EDGES_N: tuple[float, ...] = (MonitorConfig().stuck_force_n, FORCE_LIMIT_N)
DEFAULT_TORQUE_EDGES_NM: tuple[float, ...] = (InsertAct().torque_threshold_nm,)
DEFAULT_MIN_SIZE = 3
MAX_MIN_SIZE = 100_000
# Runs one survey keeps in memory (each as its replayed verdicts, not its events): far above an
# eval's (phase 3 at 10 seeds is 90), low enough that a wrong store fails fast.
MAX_SURVEY_RUNS = 20_000


@dataclass(frozen=True, order=True)
class Signature:
    skill: str
    rule: str
    force_band: str
    torque_band: str

    def label(self) -> str:
        return f"{self.skill}/{self.rule} force {self.force_band} N torque {self.torque_band} Nm"


@dataclass(frozen=True)
class Member:
    """One UNKNOWN verdict: where it was recorded and its evidence."""

    run_id: str
    seq: int  # the judged sample's event
    skill: str
    attempt: int | None
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class Group:
    signature: Signature
    members: tuple[Member, ...]
    # Referee labels: the fault kinds injected in the members' runs, one count per run.
    injected: Mapping[str, int]

    @property
    def runs(self) -> frozenset[str]:
        return frozenset(m.run_id for m in self.members)


def band(value: float | None, edges: Sequence[float]) -> str:
    """The interval of `edges` that `value` falls in: below the first, between two, or at or
    above the last."""
    if value is None:
        return "n/a"
    i = bisect.bisect_right(edges, value)
    if i == 0:
        return f"<{edges[0]:g}"
    if i == len(edges):
        return f">={edges[-1]:g}"
    return f"{edges[i - 1]:g}-{edges[i]:g}"


@dataclass(frozen=True)
class SurveyedRun:
    """What a replay keeps of one run: the replay itself and, as referee labels, the fault
    kinds injected in it. The run's events are not kept."""

    replay: RunReplay
    injected: frozenset[str]


class SurveyTooLarge(ValueError):
    """The store holds more runs than one survey keeps in memory."""


def survey(
    runs: Iterable[Sequence[Event]], config: ReplayConfig, *, max_runs: int = MAX_SURVEY_RUNS
) -> list[SurveyedRun]:
    """Replay every run, one at a time, keeping only what grouping and the report need."""
    out: list[SurveyedRun] = []
    for events in runs:
        if len(out) >= max_runs:
            raise SurveyTooLarge(f"more than {max_runs} runs in one survey")
        injected = frozenset(e.payload.kind for e in events if isinstance(e.payload, FaultInjected))
        out.append(SurveyedRun(replay_run(events, config), injected))
    return out


def signature_of(
    judged: Judged,
    force_edges: Sequence[float] = DEFAULT_FORCE_EDGES_N,
    torque_edges: Sequence[float] = DEFAULT_TORQUE_EDGES_NM,
) -> Signature | None:
    """The signature of a recorded UNKNOWN verdict, from its evidence only; None for any other
    verdict (a known class, or one that lost to the skill's own success)."""
    verdict = judged.verdict
    if judged.outcome != "recorded" or verdict.failure_class is not FailureClass.UNKNOWN:
        return None
    evidence = verdict.evidence
    torque = evidence.get("torque_nm")
    return Signature(
        skill=judged.skill,
        rule=verdict.rule,
        force_band=band(evidence.get("force_n"), force_edges),
        torque_band=band(None if torque is None else abs(torque), torque_edges),
    )


def group_unknowns(
    runs: Iterable[SurveyedRun],
    *,
    force_edges: Sequence[float] = DEFAULT_FORCE_EDGES_N,
    torque_edges: Sequence[float] = DEFAULT_TORQUE_EDGES_NM,
) -> list[Group]:
    """Every UNKNOWN verdict a run recorded, grouped by signature, largest group first.

    Only runs whose replay reproduced the record count (status `ok`): a group must be built
    from verdicts the replay can stand behind. A verdict that lost to the skill's own success
    was no failure and is left out.
    """
    if list(force_edges) != sorted(force_edges) or list(torque_edges) != sorted(torque_edges):
        raise ValueError("band edges must be in increasing order")
    members: dict[Signature, list[Member]] = {}
    kinds_of: dict[str, frozenset[str]] = {}
    for run in runs:
        replay = run.replay
        if replay.status != "ok":
            continue
        kinds_of[replay.run_id] = run.injected
        for judged in replay.judged:
            signature = signature_of(judged, force_edges, torque_edges)
            if signature is None:
                continue
            members.setdefault(signature, []).append(
                Member(
                    replay.run_id, judged.seq, judged.skill, judged.attempt, judged.verdict.evidence
                )
            )
    groups = []
    for signature, found in members.items():
        injected = Counter(
            k for run in dict.fromkeys(m.run_id for m in found) for k in kinds_of[run]
        )
        groups.append(
            Group(signature, tuple(found), MappingProxyType(dict(sorted(injected.items()))))
        )
    return sorted(groups, key=lambda g: (-len(g.members), g.signature))


def render_survey(
    runs: Sequence[SurveyedRun], groups: Sequence[Group], min_size: int
) -> tuple[list[str], bool]:
    """The lines `cellrt promote group` prints, and whether gate 0 passed. When it did not, no
    group is shown: a group is only as good as the replay behind it."""
    lines, passed, _ = gate_0(runs)
    if not passed:
        return lines, False
    lines.append(f"UNKNOWN groups, at least {min_size} verdicts:")
    lines += [_group_line(g) for g in groups if len(g.members) >= min_size] or ["  none"]
    if small := [g for g in groups if len(g.members) < min_size]:
        lines.append(f"too few (under {min_size}):")
        lines += [_group_line(g) for g in small]
    return lines, True


class Gate0(NamedTuple):
    lines: list[str]  # how many runs were replayed and with what status, then gate 0's line
    passed: bool
    summary: str  # gate 0's line without its "gate 0 <state>: "


# Statuses gate 0 refuses outright: the replay does not reproduce what the run recorded.
REFUSED_STATUSES = frozenset({"mismatch", "config_mismatch"})


def gate_0(runs: Sequence[SurveyedRun]) -> Gate0:
    """How many runs were replayed and with what status, and gate 0: no run fails to replay,
    and at least one replayed."""
    counts = Counter(r.replay.status for r in runs)
    shown = ", ".join(f"{s} {counts[s]}" for s in _STATUS_ORDER if s == "ok" or counts[s])
    lines = [f"replayed {len(runs)} runs: {shown}"]

    def result(state: str, passed: bool, summary: str) -> Gate0:
        return Gate0([*lines, f"gate 0 {state}: {summary}"], passed, summary)

    refused = [r.replay for r in runs if r.replay.status in REFUSED_STATUSES]
    if refused:
        first = refused[0]
        if first.mismatch is not None:
            m = first.mismatch
            why = f"event {m.seq}: recorded {m.recorded}, replayed {m.replayed}"
        else:
            why = f"its {first.detail} configuration is not the one replayed with"
        return result(
            "refused", False, f"{len(refused)} run(s) do not replay; first {first.run_id}, {why}"
        )
    if not counts["ok"]:
        return result("not run", False, "no run in the store could be replayed")
    verdicts = sum(1 for r in runs for j in r.replay.judged if j.outcome == "recorded")
    timeouts = sum(r.replay.timeouts for r in runs)
    return result(
        "passed",
        True,
        f"every recorded verdict reproduced ({verdicts} verdicts in {counts['ok']} runs; "
        f"{timeouts} skill timeouts, decided by the clock)",
    )


_STATUS_ORDER = ("ok", "mismatch", "config_mismatch", "unfinished", "unjudgeable")


def _group_line(group: Group) -> str:
    referee = ", ".join(f"{kind} {n}" for kind, n in group.injected.items()) or "none"
    size = f"{len(group.members)} verdicts in {len(group.runs)} runs"
    return f"  {size}  {group.signature.label()}  (referee: {referee})"
