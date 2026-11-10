"""Approving a validated candidate and loading approved rules (ADR 021, ticket T3).

An approval needs a report whose gates all passed and whose candidate still has the hash the
report names; the approved file is what an eval loads, never the report or the candidate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.classifier import FailureClass, MonitorConfig
from cellrt.monitor.promoted import Candidate
from cellrt.promotion.adopt import (
    MAX_ADOPTED_CHARS,
    MAX_GROUP_CHARS,
    MAX_PROMOTED_RULES,
    MAX_REPORT_CHARS,
    PROMOTED_UNKNOWN_BUDGET,
    Adopted,
    ApprovalRefused,
    approve,
    load_promoted,
    with_promoted,
    write_adopted,
)
from cellrt.promotion.replay import ReplayConfig
from cellrt.skills.mock import mock_registry

RULE: dict[str, Any] = {
    "name": "PLUG_ROTATED",
    "skill": "insert_scripted",
    "window_s": 0.5,
    "conditions": [
        {"field": "torque_nm", "op": ">=", "value": 0.3},
        {"field": "progress_mm", "op": "<", "value": 0.2},
    ],
}
PLAN: dict[str, Any] = {"action": "switch_skill", "skill": "insert_act"}


def _candidate(**rule: Any) -> Candidate:
    return Candidate.parse(json.dumps({"rule": {**RULE, **rule}, "plan": PLAN}))


def _gate(n: Any, passed: Any = True, failed: Any = 0, cases: Any = ()) -> dict[str, Any]:
    return {"gate": n, "passed": passed, "summary": "s", "failed": failed, "cases": list(cases)}


GATES = [_gate(n) for n in range(4)]


def _report(validated: Candidate | None = None, **over: Any) -> str:
    """A report as `cellrt promote validate --out` writes it (the fields approval reads)."""
    validated = validated or _candidate()
    data: dict[str, Any] = {
        "report_version": 1,
        "candidate": validated.model_dump(mode="json"),
        "candidate_hash": validated.digest(),
        "group": "insert_scripted/insert_stalled force <10 N torque >=0.1 Nm",
        "config_parts": dict(ReplayConfig.mock().parts),
        "gates": GATES,
        "passed": True,
    }
    data.update(over)
    return json.dumps(data)


def test_a_passed_report_is_approved_by_a_named_person() -> None:
    report = _report()
    adopted = approve(report, "Shaun Chen", mock_registry())
    assert adopted.rule.name == "PLUG_ROTATED"
    assert adopted.plan.action == "switch_skill"
    assert adopted.approved_by == "Shaun Chen"
    assert adopted.candidate_hash == _candidate().digest()
    assert len(adopted.report_hash) == 16


@pytest.mark.parametrize(
    "over, code",
    [
        ({"passed": False}, "not_passed"),
        ({"gates": [_gate(n, passed=n != 2) for n in range(4)]}, "gate_failed"),
        ({"gates": [*GATES[:3], _gate(3, failed=1)]}, "gate_failed"),
        ({"gates": [*GATES[:3], _gate(3, cases=[{"run_id": "r"}])]}, "gate_failed"),
        ({"gates": GATES[:3]}, "gate_missing"),
        ({"gates": [*GATES, _gate(2)]}, "gate_missing"),  # a gate given twice
        ({"gates": [*GATES[:3], _gate(2)]}, "gate_missing"),
        ({"gates": [*GATES[:3], _gate(True)]}, "gates_invalid"),
        ({"gates": [*GATES[:3], _gate(3.0)]}, "gates_invalid"),
        ({"gates": [*GATES[:3], _gate([3])]}, "gates_invalid"),
        ({"gates": [*GATES[:3], _gate(3, passed=1)]}, "gates_invalid"),
        ({"gates": {"0": True}}, "gates_invalid"),
        ({"gates": [*GATES[:3], [3, True]]}, "gates_invalid"),
        ({"candidate_hash": "0" * 16}, "candidate_hash_mismatch"),
        ({"report_version": 2}, "report_version"),
        ({"report_version": True}, "report_version"),
        ({"candidate": {"rule": {"name": "x"}, "plan": {}}}, "candidate_refused"),
        ({"config_parts": {**ReplayConfig.mock().parts, "monitor": "0" * 16}}, "config_stale"),
        ({"config_parts": None}, "config_stale"),
        ({"group": "g" * (MAX_GROUP_CHARS + 1)}, "group_invalid"),
        ({"group": ""}, "group_invalid"),
        ({"group": 7}, "group_invalid"),
        ({"pad": "x" * MAX_REPORT_CHARS}, "report_too_long"),
    ],
)
def test_a_report_that_does_not_stand_is_refused(over: dict[str, Any], code: str) -> None:
    with pytest.raises(ApprovalRefused, match=code):
        approve(_report(**over), "Shaun", mock_registry())


def test_a_candidate_changed_after_its_report_is_refused() -> None:
    # The report's candidate edited by hand: its hash no longer matches.
    report = json.loads(_report())
    report["candidate"]["rule"]["window_s"] = 0.6
    with pytest.raises(ApprovalRefused, match="candidate_hash_mismatch"):
        approve(json.dumps(report), "Shaun", mock_registry())


@pytest.mark.parametrize(
    "raw, code",
    [
        ("not json", "report_not_json"),
        ("[1, 2]", "report_not_an_object"),
        ("[" * 100_000, "report_not_json"),
    ],
)
def test_a_report_that_is_not_one_is_refused(raw: str, code: str) -> None:
    with pytest.raises(ApprovalRefused, match=code):
        approve(raw, "Shaun", mock_registry())


@pytest.mark.parametrize("by", ["", " Shaun", "a" * 65, "Shaun\nForged", "<script>"])
def test_the_approver_is_a_plain_name(by: str) -> None:
    with pytest.raises(ApprovalRefused, match="approver"):
        approve(_report(), by, mock_registry())


def test_an_approved_rule_is_written_once(tmp_path: Path) -> None:
    adopted = approve(_report(), "Shaun", mock_registry())
    path = write_adopted(adopted, tmp_path)
    assert path == tmp_path / "PLUG_ROTATED.json"
    assert Adopted.model_validate_json(path.read_text(encoding="utf-8")) == adopted
    with pytest.raises(ApprovalRefused, match="already_adopted"):
        write_adopted(adopted, tmp_path)


@pytest.mark.parametrize("name", ["CON", "NUL", "AUX", "PRN", "COM1", "LPT9"])
def test_a_rule_named_like_a_windows_device_is_not_written(tmp_path: Path, name: str) -> None:
    # `NUL.json` names the null device on Windows: the approval would be written nowhere.
    adopted = approve(_report(_candidate(name=name)), "Shaun", mock_registry())
    with pytest.raises(ApprovalRefused, match="rule_name_reserved"):
        write_adopted(adopted, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_write_that_fails_leaves_no_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted = approve(_report(), "Shaun", mock_registry())
    real_open = Path.open

    class _Full:  # the file opens, then the disk is full
        def __init__(self, f: Any) -> None:
            self._f = f

        def __enter__(self) -> _Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self._f.close()

        def write(self, text: str) -> int:
            raise OSError("no space left on device")

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: _Full(real_open(self, *a, **k)))
    with pytest.raises(OSError):
        write_adopted(adopted, tmp_path)
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []
    assert write_adopted(adopted, tmp_path).is_file()  # the name is still free


def _adopt(tmp_path: Path, **rule: Any) -> None:
    write_adopted(approve(_report(_candidate(**rule)), "Shaun", mock_registry()), tmp_path)


def _band(i: int) -> list[dict[str, Any]]:
    """Conditions no other band's rule meets on the same sample: torque in [i, i + 1) Nm."""
    return [
        {"field": "torque_nm", "op": ">=", "value": float(i)},
        {"field": "torque_nm", "op": "<", "value": float(i + 1)},
    ]


def test_approved_rules_load_in_name_order(tmp_path: Path) -> None:
    _adopt(tmp_path, name="ZED_RULE", conditions=_band(0))
    _adopt(tmp_path, name="ALPHA_RULE", conditions=_band(1))
    promoted = load_promoted(tmp_path, mock_registry())
    assert [r.name for r in promoted.rules] == ["ALPHA_RULE", "ZED_RULE"]
    assert set(promoted.plans) == {"ALPHA_RULE", "ZED_RULE"}


def test_the_load_order_is_the_names_as_strings_on_every_platform(tmp_path: Path) -> None:
    # Windows paths compare case-folded, which puts "_" before letters; strings do not.
    names = ["A_B_RULE", "AB_RULE", "ABC"]
    for i, name in enumerate(names):
        _adopt(tmp_path, name=name, conditions=_band(i))
    assert [r.name for r in load_promoted(tmp_path, mock_registry()).rules] == sorted(names)


def test_rules_that_can_fire_on_the_same_sample_are_not_loaded(tmp_path: Path) -> None:
    # The load order would pick between them, and the order is only the names (ADR 021).
    _adopt(tmp_path, name="PLUG_ROTATED")
    _adopt(tmp_path, name="PLUG_TWISTED", conditions=[RULE["conditions"][0]])
    with pytest.raises(ApprovalRefused, match="rules_overlap"):
        load_promoted(tmp_path, mock_registry())


def test_a_candidate_that_overlaps_an_approved_rule_is_refused(tmp_path: Path) -> None:
    _adopt(tmp_path)
    approved = load_promoted(tmp_path, mock_registry()).rules
    report = _report(_candidate(name="PLUG_TWISTED", conditions=_band(0)))  # 0.3 Nm meets both
    with pytest.raises(ApprovalRefused, match="rules_overlap"):
        approve(report, "Shaun", mock_registry(), approved=approved)


def test_a_candidate_apart_from_the_approved_rules_is_approved(tmp_path: Path) -> None:
    _adopt(tmp_path, conditions=_band(0))
    approved = load_promoted(tmp_path, mock_registry()).rules
    report = _report(_candidate(name="PLUG_TWISTED", conditions=_band(1)))
    assert approve(report, "Shaun", mock_registry(), approved=approved).rule.name == "PLUG_TWISTED"


def test_the_same_rule_again_is_left_to_the_write(tmp_path: Path) -> None:
    # Not an overlap with itself: the write refuses to replace it, with its own code.
    _adopt(tmp_path)
    approved = load_promoted(tmp_path, mock_registry()).rules
    adopted = approve(_report(), "Shaun", mock_registry(), approved=approved)
    with pytest.raises(ApprovalRefused, match="already_adopted"):
        write_adopted(adopted, tmp_path)


def test_only_files_ending_in_lower_case_json_are_read(tmp_path: Path) -> None:
    # Windows' glob would match `.JSON` too; Linux's would not. Neither reads it.
    _adopt(tmp_path)
    (tmp_path / "PLUG_ROTATED.json").rename(tmp_path / "PLUG_ROTATED.JSON")
    assert load_promoted(tmp_path, mock_registry()).rules == ()


def test_a_directory_that_cannot_be_listed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path, pattern: str) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "glob", denied)
    with pytest.raises(ApprovalRefused, match="adopted_unreadable"):
        load_promoted(tmp_path, mock_registry())


def _rewrite(path: Path, **changes: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_rule_approved_under_another_monitor_configuration_is_refused(tmp_path: Path) -> None:
    _adopt(tmp_path)
    parts = {**ReplayConfig.mock().parts, "monitor": "0" * 16}
    _rewrite(tmp_path / "PLUG_ROTATED.json", config_parts=parts)
    with pytest.raises(ApprovalRefused, match="config_stale"):
        load_promoted(tmp_path, mock_registry())


def test_a_consistent_file_naming_an_unregistered_skill_is_refused(tmp_path: Path) -> None:
    # Edited by hand with its hash recomputed: the registry check on load still stops it.
    _adopt(tmp_path)
    plan = {"action": "switch_skill", "skill": "insert_fancy"}
    edited = Candidate.parse(json.dumps({"rule": RULE, "plan": plan}))
    _rewrite(tmp_path / "PLUG_ROTATED.json", plan=plan, candidate_hash=edited.digest())
    with pytest.raises(ApprovalRefused, match="candidate_refused"):
        load_promoted(tmp_path, mock_registry())


def test_an_approved_file_is_read_bounded(tmp_path: Path) -> None:
    _adopt(tmp_path)
    path = tmp_path / "PLUG_ROTATED.json"
    path.write_text(path.read_text(encoding="utf-8") + " " * MAX_ADOPTED_CHARS, encoding="utf-8")
    with pytest.raises(ApprovalRefused, match="adopted_too_long"):
        load_promoted(tmp_path, mock_registry())


def test_an_empty_or_missing_directory_loads_nothing(tmp_path: Path) -> None:
    assert load_promoted(tmp_path, mock_registry()).rules == ()
    with pytest.raises(ApprovalRefused, match="no_directory"):
        load_promoted(tmp_path / "absent", mock_registry())


def test_a_file_whose_name_is_not_its_rule_is_refused(tmp_path: Path) -> None:
    _adopt(tmp_path)
    (tmp_path / "PLUG_ROTATED.json").rename(tmp_path / "OTHER_NAME.json")
    with pytest.raises(ApprovalRefused, match="file_name"):
        load_promoted(tmp_path, mock_registry())


def test_a_tampered_file_is_refused(tmp_path: Path) -> None:
    _adopt(tmp_path)
    path = tmp_path / "PLUG_ROTATED.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["rule"]["window_s"] = 0.6  # no longer the candidate that was validated
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ApprovalRefused, match="candidate_hash_mismatch"):
        load_promoted(tmp_path, mock_registry())


def test_the_number_of_rules_is_bounded(tmp_path: Path) -> None:
    for i in range(MAX_PROMOTED_RULES + 1):
        _adopt(tmp_path, name=f"RULE_{i:02d}")
    with pytest.raises(ApprovalRefused, match="too_many_rules"):
        load_promoted(tmp_path, mock_registry())


def test_rules_and_plans_go_into_the_monitor_and_recovery(tmp_path: Path) -> None:
    _adopt(tmp_path)
    monitor, recovery = with_promoted(
        load_promoted(tmp_path, mock_registry()), MonitorConfig(), RecoveryConfig()
    )
    assert [r.name for r in monitor.promoted] == ["PLUG_ROTATED"]
    assert recovery.promoted["PLUG_ROTATED"].skill == "insert_act"
    # A promoted plan counts against the UNKNOWN budget, which has none by default.
    assert recovery.budgets[FailureClass.UNKNOWN.value] == PROMOTED_UNKNOWN_BUDGET
    assert "promoted" in recovery.as_dict()  # so the hash tells the two configs apart


def test_an_explicit_unknown_budget_is_kept(tmp_path: Path) -> None:
    _adopt(tmp_path)
    base = RecoveryConfig(budgets={**RecoveryConfig().budgets, FailureClass.UNKNOWN.value: 1})
    _, recovery = with_promoted(load_promoted(tmp_path, mock_registry()), MonitorConfig(), base)
    assert recovery.budgets[FailureClass.UNKNOWN.value] == 1


def test_without_rules_the_configs_are_left_as_they_were(tmp_path: Path) -> None:
    monitor, recovery = with_promoted(
        load_promoted(tmp_path, mock_registry()), MonitorConfig(), RecoveryConfig()
    )
    assert monitor == MonitorConfig()
    assert recovery == RecoveryConfig()
    assert recovery.as_dict() == RecoveryConfig().as_dict()
