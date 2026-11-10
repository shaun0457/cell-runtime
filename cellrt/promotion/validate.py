"""Validate a candidate rule by replay (ADR 022, gates 0-3; ticket T2).

Gate 0 comes first: without the candidate, the replay reproduces every recorded verdict
(`groups.gate_0`). Then each run is replayed again with the candidate (none once gate 0 has
refused a run, since no other gate runs then), and the first verdict of
each watch window (a skill attempt, or a hold) is compared with the first verdict the run
recorded in it. That verdict ended the window; what the candidate would have changed after an
earlier verdict of its own is not in the log, so only first verdicts are compared.

- Gate 1, coverage: the candidate is the first verdict of every window of its group, which has
  at least `min_size` of them.
- Gate 2, no false alarms: it is the first verdict of no window in a run without an injected
  fault, and the store has such runs. That includes the group's own windows: in a run without a
  fault the mock has no referee to tell a false alarm from a natural failure (ADR 015), so a
  group drawn from such runs cannot pass.
- Gate 3, no theft: every other window's first verdict is unchanged.

A verdict that lost to the watched skill's own success on the same tick changed nothing in the
run, so it is no window's first verdict, recorded or replayed. Replay checks the rule, not the
plan: the recorded runs never ran it. The report says so.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cellrt.monitor.promoted import Candidate, CandidateRefused
from cellrt.promotion.groups import (
    DEFAULT_MIN_SIZE,
    MAX_SURVEY_RUNS,
    REFUSED_STATUSES,
    Group,
    SurveyedRun,
    SurveyTooLarge,
    gate_0,
    group_unknowns,
    signature_of,
    survey,
)
from cellrt.promotion.replay import Judged, ReplayConfig, RunReplay, replay_run
from cellrt.runtime.events import Event
from cellrt.skills.registry import SkillRegistry

MAX_CASES = 5  # failing windows kept per gate
REPORT_VERSION = 2  # 2: the report records the group size gate 1 asked for
NOT_CHECKED = (
    "replay checks the rule, not the plan: the recorded runs never ran the plan, so only a new "
    "eval run with the rule adopted shows whether it works"
)


class GroupNotFound(ValueError):
    """The store has no UNKNOWN group with that label."""


@dataclass(frozen=True)
class Case:
    """A window that fails a gate: what the run recorded first in it and what the replay with
    the candidate gave first."""

    run_id: str
    skill: str
    attempt: int | None
    seq: int  # the sample of the first verdict that differs
    recorded: str
    replayed: str

    def line(self) -> str:
        attempt = "hold" if self.attempt is None else f"a{self.attempt}"
        return (
            f"  {self.run_id} {self.skill} {attempt}, event {self.seq}: "
            f"recorded {self.recorded}, replayed {self.replayed}"
        )


@dataclass(frozen=True)
class Gate:
    number: int
    passed: bool | None  # None: not run, because gate 0 did not pass
    summary: str
    cases: tuple[Case, ...] = ()  # the first MAX_CASES failing windows
    failed: int = 0  # every failing window

    def line(self) -> str:
        state = {True: "passed", False: "failed", None: "not run"}[self.passed]
        more = (
            f" ({self.failed - len(self.cases)} more not shown)" if self.failed > MAX_CASES else ""
        )
        return f"gate {self.number} {state}: {self.summary}{more}"


@dataclass(frozen=True)
class Report:
    candidate: Candidate
    group: str
    parts: Mapping[str, str]  # the configuration hashes the runs were replayed with
    runs: tuple[str, ...]  # the runs gate 0 replayed
    skipped: Mapping[str, int]  # runs not replayed, by status
    replayed: tuple[str, ...]  # gate 0's lines: what was replayed
    gates: tuple[Gate, ...]
    min_size: int = DEFAULT_MIN_SIZE  # the windows gate 1 asked the group for

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def lines(self) -> list[str]:
        rule = self.candidate.rule
        out = [f"candidate {rule.name} ({self.candidate.digest()}) for {self.group}"]
        out += self.replayed
        for gate in self.gates:
            if gate.number:  # gate 0's own line is in `replayed`
                out.append(gate.line())
            out += [case.line() for case in gate.cases]
        failed = [str(g.number) for g in self.gates if g.passed is False]  # not those not run
        out.append("passed" if self.passed else f"failed: gate {', '.join(failed)}")
        out.append(f"note: {NOT_CHECKED}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_version": REPORT_VERSION,
            "candidate": self.candidate.model_dump(mode="json"),
            "candidate_hash": self.candidate.digest(),
            "group": self.group,
            "config_parts": dict(self.parts),
            "min_size": self.min_size,
            "runs": list(self.runs),
            "skipped": dict(self.skipped),
            "gates": [
                {
                    "gate": g.number,
                    "passed": g.passed,
                    "summary": g.summary,
                    "failed": g.failed,
                    "cases": [vars(c) for c in g.cases],
                }
                for g in self.gates
            ],
            "passed": self.passed,
            "not_checked": NOT_CHECKED,
        }


def check_registry(candidate: Candidate, registry: SkillRegistry) -> None:
    """The rule watches a registered insertion skill, and a switch goes to another one. The
    refusal names the field and a code, never the value (it comes from a model)."""

    def insertion(name: str, where: str) -> None:
        if name not in registry:
            raise CandidateRefused(f"candidate refused: {where}: not_registered")
        if registry.get(name).monitor_profile != "insertion":
            raise CandidateRefused(f"candidate refused: {where}: not_an_insertion_skill")

    insertion(candidate.rule.skill, "rule.skill")
    plan = candidate.plan
    if plan.action == "switch_skill":
        assert plan.skill is not None  # the plan's own validator
        insertion(plan.skill, "plan.skill")
        if plan.skill == candidate.rule.skill:
            raise CandidateRefused("candidate refused: plan.skill: same_as_rule_skill")


def validate(
    runs: Iterable[Sequence[Event]],
    candidate: Candidate,
    group: str,
    config: ReplayConfig,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_runs: int = MAX_SURVEY_RUNS,
) -> Report:
    """Gates 0-3 for `candidate` against the group labelled `group`, over every run in `runs`
    (read once; each run is replayed without and with the candidate)."""
    surveyed: list[SurveyedRun] = []
    with_candidate: dict[str, RunReplay] = {}
    refused = False  # gate 0 has refused a run: no other gate runs
    for events in runs:
        if len(surveyed) >= max_runs:
            raise SurveyTooLarge(f"more than {max_runs} runs in one survey")
        [run] = survey([events], config)
        surveyed.append(run)
        refused = refused or run.replay.status in REFUSED_STATUSES
        if refused:
            with_candidate.clear()  # nobody reads these any more
        elif run.replay.status == "ok":
            with_candidate[run.replay.run_id] = replay_run(events, config, candidate=candidate.rule)

    replayed, passed, gate_0_summary = gate_0(surveyed)
    statuses = Counter(r.replay.status for r in surveyed)

    def report(*gates: Gate) -> Report:
        return Report(
            candidate=candidate,
            group=group,
            parts=dict(config.parts),
            runs=tuple(r.replay.run_id for r in surveyed if r.replay.status == "ok"),
            skipped={s: n for s, n in sorted(statuses.items()) if s != "ok"},
            replayed=tuple(replayed),
            gates=gates,
            min_size=min_size,
        )

    if not passed:
        why = "gate 0 did not pass"
        return report(Gate(0, False, gate_0_summary), *(Gate(n, None, why) for n in (1, 2, 3)))

    [target] = [g for g in group_unknowns(surveyed) if g.signature.label() == group] or [None]
    if target is None:
        raise GroupNotFound("no UNKNOWN group with that label in the store")
    gates = _Gates(target, candidate.rule.verdict_rule, config.period_s, min_size)
    for run in surveyed:
        if run.replay.status == "ok":
            gates.add(run, with_candidate[run.replay.run_id])
    return report(Gate(0, True, gate_0_summary), *gates.results())


class _Gates:
    """Gates 1-3, window by window."""

    def __init__(self, target: Group, rule: str, period_s: float, min_size: int) -> None:
        self._target = target
        self._rule = rule
        self._period_s = period_s
        self._min_size = min_size
        self._members = 0
        self._leads: list[int] = []  # samples the candidate came before the recorded UNKNOWN
        self._clean_runs = 0
        self._other = 0
        self._cases: dict[int, list[Case]] = {1: [], 2: [], 3: []}

    def add(self, run: SurveyedRun, replayed: RunReplay) -> None:
        clean = not run.injected
        self._clean_runs += clean
        # A recorded verdict ends its window (the executor preempts on it, ADR 006), so the
        # candidate's first verdict in a window is never on a later sample than the recorded one.
        recorded = _firsts(j for j in run.replay.judged if j.outcome == "recorded")
        candidates = _firsts(j for j in replayed.judged if j.outcome == "replayed")
        for window in sorted(set(recorded) | set(candidates)):
            first, mine = recorded.get(window), candidates.get(window)
            fired = mine is not None and mine.verdict.rule == self._rule
            if first is not None and signature_of(first) == self._target.signature:
                self._members += 1
                if fired:
                    assert mine is not None and first is not None
                    self._leads.append(first.step - mine.step)
                    if clean:  # gate 2 counts the group's own windows too (see the docstring)
                        self._fail(2, run, first, mine)
                else:
                    self._fail(1, run, first, mine)
            elif fired:
                self._fail(2 if clean else 3, run, first, mine)
            else:
                self._other += 1
                if not _same(first, mine):  # never expected: built-in rules are unchanged
                    self._fail(3, run, first, mine)

    def _fail(self, gate: int, run: SurveyedRun, first: Judged | None, mine: Judged | None) -> None:
        where = mine if first is None or (mine is not None and mine.seq < first.seq) else first
        assert where is not None
        self._cases[gate].append(
            Case(
                run.replay.run_id,
                where.skill,
                where.attempt,
                where.seq,
                recorded=_named(first),
                replayed=_named(mine),
            )
        )

    def results(self) -> tuple[Gate, Gate, Gate]:
        missed = self._cases[1]
        covered = self._members - len(missed)
        if self._members < self._min_size:
            g1 = Gate(1, False, f"the group has {self._members} windows, under {self._min_size}")
        else:
            lead = ""
            if self._leads:
                lead_s = statistics.median(self._leads) * self._period_s
                lead = f", a median {lead_s:.2f} s before the recorded UNKNOWN"
            g1 = self._gate(
                1, f"first verdict in {covered} of {self._members} windows of the group{lead}"
            )
        if not self._clean_runs:
            g2 = Gate(2, False, "no run without an injected fault: false alarms cannot be checked")
        else:
            alarmed = len({c.run_id for c in self._cases[2]})
            g2 = self._gate(
                2,
                f"first verdict in {alarmed} of {self._clean_runs} runs without an injected fault",
            )
        changed = len(self._cases[3])
        g3 = self._gate(
            3,
            f"{changed} of {self._other + changed} other windows with a verdict, recorded or "
            "replayed, changed their first verdict",
        )
        return g1, g2, g3

    def _gate(self, number: int, summary: str) -> Gate:
        cases = self._cases[number]
        return Gate(number, not cases, summary, tuple(cases[:MAX_CASES]), len(cases))


def _firsts(judged: Iterable[Judged]) -> dict[int, Judged]:
    """The first verdict of each window."""
    out: dict[int, Judged] = {}
    for j in judged:
        out.setdefault(j.window, j)
    return out


def _same(a: Judged | None, b: Judged | None) -> bool:
    if a is None or b is None:
        return a is b
    return (a.seq, a.verdict.failure_class, a.verdict.rule) == (
        b.seq,
        b.verdict.failure_class,
        b.verdict.rule,
    )


def _named(j: Judged | None) -> str:
    return "none" if j is None else f"{j.verdict.failure_class.value}/{j.verdict.rule}"
