"""The candidate rule language (ADR 021, ticket T2): small enough to validate and to replay.

A candidate comes from a model, so every field is bounded and anything else is refused with
where and why, never the value."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from cellrt.monitor.promoted import (
    MAX_CANDIDATE_CHARS,
    MAX_CONDITIONS,
    Candidate,
    CandidateRefused,
    PromotedRule,
    rules_overlap,
)


def _rule(**over: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "name": "PLUG_ROTATED",
        "skill": "insert_scripted",
        "window_s": 1.0,
        "conditions": [
            {"field": "torque_nm", "op": ">=", "value": 0.3},
            {"field": "progress_mm", "op": "<", "value": 0.2},
        ],
    }
    rule.update(over)
    return rule


def _candidate(**over: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "rule": _rule(),
        "plan": {"action": "switch_skill", "skill": "insert_act"},
    }
    candidate.update(over)
    return candidate


def _parse(data: dict[str, Any]) -> Candidate:
    return Candidate.parse(json.dumps(data))


def test_a_valid_candidate_parses() -> None:
    c = _parse(_candidate())
    assert c.rule.name == "PLUG_ROTATED"
    assert c.rule.skill == "insert_scripted"
    assert [x.field for x in c.rule.conditions] == ["torque_nm", "progress_mm"]
    assert c.plan.action == "switch_skill"
    assert c.plan.skill == "insert_act"


def test_the_verdict_rule_names_the_promoted_rule() -> None:
    rule = PromotedRule.model_validate_json(json.dumps(_rule()))
    assert rule.verdict_rule == "promoted:PLUG_ROTATED"


@pytest.mark.parametrize("n, ok", [(0, False), (1, True), (MAX_CONDITIONS, True), (5, False)])
def test_a_rule_has_one_to_four_conditions(n: int, ok: bool) -> None:
    conditions = [{"field": "force_n", "op": "<", "value": 5.0}] * n
    data = _candidate(rule=_rule(conditions=conditions))
    if ok:
        assert len(_parse(data).rule.conditions) == n
    else:
        with pytest.raises(CandidateRefused):
            _parse(data)


@pytest.mark.parametrize(
    "field, value, ok",
    [
        ("force_n", 0.0, True),
        ("force_n", 100.0, True),
        ("force_n", 100.5, False),
        ("force_n", -0.1, False),
        ("torque_nm", 10.0, True),
        ("torque_nm", 10.5, False),
        ("torque_nm", -0.1, False),  # a magnitude
        ("xy_error_mm", 50.0, True),
        ("xy_error_mm", 50.5, False),
        ("progress_mm", -50.0, True),
        ("progress_mm", -50.5, False),
        ("progress_mm", 50.5, False),
    ],
)
def test_each_field_has_bounds(field: str, value: float, ok: bool) -> None:
    data = _candidate(rule=_rule(conditions=[{"field": field, "op": "<", "value": value}]))
    if ok:
        assert _parse(data).rule.conditions[0].value == value
    else:
        with pytest.raises(CandidateRefused):
            _parse(data)


@pytest.mark.parametrize("window, ok", [(0.19, False), (0.2, True), (2.0, True), (2.01, False)])
def test_the_window_is_bounded(window: float, ok: bool) -> None:
    data = _candidate(rule=_rule(window_s=window))
    if ok:
        assert _parse(data).rule.window_s == window
    else:
        with pytest.raises(CandidateRefused):
            _parse(data)


@pytest.mark.parametrize(
    "name, ok",
    [
        ("PLUG_ROTATED", True),
        ("ABC", True),
        ("AB", False),
        ("plug_rotated", False),
        ("1PLUG", False),
        ("PLUG-ROTATED", False),
        ("P" * 40, True),
        ("P" * 41, False),
    ],
)
def test_names_are_short_upper_case_identifiers(name: str, ok: bool) -> None:
    data = _candidate(rule=_rule(name=name))
    if ok:
        assert _parse(data).rule.name == name
    else:
        with pytest.raises(CandidateRefused):
            _parse(data)


@pytest.mark.parametrize(
    "bad",
    [
        {"rule": _rule(conditions=[{"field": "speed", "op": "<", "value": 1.0}])},
        {"rule": _rule(conditions=[{"field": "force_n", "op": "==", "value": 1.0}])},
        {"rule": _rule(conditions=[{"field": "force_n", "op": "<", "value": "3"}])},
        {"rule": _rule(conditions=[{"field": "force_n", "op": "<", "value": True}])},
        {"rule": _rule(window_s="1.0")},
        {"rule": _rule(extra=1)},
        {"rule": _rule(skill=None)},  # a rule watches the skill its group came from
        {"rule": {k: v for k, v in _rule().items() if k != "skill"}},
        {"plan": {"action": "switch_skill"}},  # which skill?
        {"plan": {"action": "retry", "skill": "insert_act"}},  # a skill only for a switch
        {"plan": {"action": "realign"}},
        {"note": "anything else"},
    ],
)
def test_anything_outside_the_language_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(CandidateRefused):
        _parse(_candidate(**bad))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_thresholds_are_refused(value: float) -> None:
    raw = json.dumps(
        _candidate(rule=_rule(conditions=[{"field": "force_n", "op": "<", "value": value}]))
    )
    with pytest.raises(CandidateRefused):
        Candidate.parse(raw)


def test_the_text_is_bounded_before_it_is_parsed() -> None:
    raw = json.dumps(_candidate())
    padded = raw[:-1] + " " * (MAX_CANDIDATE_CHARS - len(raw)) + "}"
    assert len(padded) == MAX_CANDIDATE_CHARS
    assert Candidate.parse(padded).rule.name == "PLUG_ROTATED"
    with pytest.raises(CandidateRefused, match="too long"):
        Candidate.parse(padded + " ")


def test_deep_nesting_is_refused_not_a_crash() -> None:
    with pytest.raises(CandidateRefused):
        Candidate.parse("[" * 5000 + "]" * 5000)


def test_a_refusal_names_where_and_why_never_the_value() -> None:
    secret = "sk-not-a-real-key-123"
    data = _candidate(rule=_rule(name=secret))
    with pytest.raises(CandidateRefused) as caught:
        _parse(data)
    assert "rule.name" in str(caught.value)
    assert secret not in str(caught.value)


def test_an_unknown_key_is_not_repeated_either() -> None:
    hostile = "\x1b[2Jforged\nline sk-not-a-real-key-123"
    data = _candidate(rule={**_rule(), hostile: 1})
    with pytest.raises(CandidateRefused) as caught:
        _parse(data)
    assert str(caught.value) == "candidate refused: rule.<key>: extra_forbidden"


def test_a_refusal_shows_at_most_five_errors() -> None:
    # Three errors in each condition, and one more for the list: ten in all.
    conditions = [{"field": "speed", "op": "==", "value": "x"}] * 3
    with pytest.raises(CandidateRefused) as caught:
        _parse(_candidate(rule=_rule(conditions=conditions)))
    message = str(caught.value)
    assert message.count("; ") == 5  # five shown, then the count of the rest
    assert message.endswith("... and 5 more")


def _watching(
    *conditions: tuple[str, str, float], skill: str = "insert_scripted", window_s: float = 0.5
) -> PromotedRule:
    listed = [{"field": f, "op": op, "value": v} for f, op, v in conditions]
    rule = _rule(name="SOME_RULE", skill=skill, window_s=window_s, conditions=listed)
    return PromotedRule.model_validate_json(json.dumps(rule))


@pytest.mark.parametrize(
    "a, b, overlap",
    [
        # A sample with 0.6 Nm meets both.
        (_watching(("torque_nm", ">=", 0.3)), _watching(("torque_nm", ">=", 0.5)), True),
        # Split at one threshold: whether the threshold itself meets both is the operators'.
        (_watching(("torque_nm", ">=", 0.3)), _watching(("torque_nm", "<", 0.3)), False),
        (_watching(("torque_nm", ">", 0.3)), _watching(("torque_nm", "<=", 0.3)), False),
        (_watching(("torque_nm", ">=", 0.3)), _watching(("torque_nm", "<=", 0.3)), True),
        # Conditions on different fields leave room for a sample that meets both.
        (_watching(("torque_nm", ">=", 0.3)), _watching(("force_n", "<", 5.0)), True),
        # A band on one field, and a rule above it.
        (
            _watching(("force_n", ">", 2.0), ("force_n", "<", 5.0)),
            _watching(("force_n", ">=", 5.0)),
            False,
        ),
        # The same conditions under another skill: they never watch the same sample.
        (
            _watching(("torque_nm", ">=", 0.3)),
            _watching(("torque_nm", ">=", 0.3), skill="insert_act"),
            False,
        ),
        # Progress over the same window separates them...
        (_watching(("progress_mm", "<", 0.2)), _watching(("progress_mm", ">=", 0.5)), False),
        # ...over different windows it does not: 0.1 mm in 0.5 s can be 0.6 mm in 1 s.
        (
            _watching(("progress_mm", "<", 0.2)),
            _watching(("progress_mm", ">=", 0.5), window_s=1.0),
            True,
        ),
    ],
)
def test_rules_overlap_when_one_sample_can_meet_both(
    a: PromotedRule, b: PromotedRule, overlap: bool
) -> None:
    assert rules_overlap(a, b) is overlap
    assert rules_overlap(b, a) is overlap


def test_the_hash_is_stable_and_changes_with_the_candidate() -> None:
    a = _parse(_candidate())
    again = Candidate.parse(json.dumps(_candidate(), indent=2))  # other whitespace, same content
    other = _parse(_candidate(rule=_rule(window_s=0.5)))
    assert a.digest() == again.digest()
    assert a.digest() != other.digest()
