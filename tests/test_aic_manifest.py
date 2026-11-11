"""The scene manifest the policy reads (`adapters/aic/manifest.py`, ADR 027): strict, bounded in
size, and refused with fixed messages that never repeat what the file holds."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from adapters.aic.manifest import (
    MANIFEST_INVALID,
    MANIFEST_TOO_LARGE,
    MANIFEST_UNREADABLE,
    MAX_BYTES,
    MAX_UNITS,
    TASK_MISMATCH,
    TASK_NOT_IN_MANIFEST,
    Manifest,
    ManifestError,
    Placement,
    load_manifest,
    scene_digest,
)

SECRET = "sk-123-not-for-logs"
TRIAL: dict[str, Any] = {
    "scene": {"task_board": {"pose": {"x": 0.15}}},
    "task": {"port_name": "sfp_port_0", "time_limit": 180},
}


def manifest_data(name: str = "pilot-v1", repeats: int = 2) -> dict[str, Any]:
    digest = scene_digest(TRIAL)
    return {
        "name": name,
        "version": 1,
        "scenes": {
            "nominal_sfp": {
                "family": "nominal",
                "digest": digest,
                "hypothesis": "the base scene succeeds",
                "expected_class": "unknown",
                "trial": TRIAL,
            }
        },
        "units": {f"nominal_sfp_r{k}": {"scene": "nominal_sfp", "seed": k} for k in range(repeats)},
    }


def write(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), "utf-8")
    return path


def test_a_manifest_places_each_task_id_in_its_scene_and_repeat(tmp_path: Path) -> None:
    manifest = load_manifest(write(tmp_path / "m.json", manifest_data()))

    assert manifest.lookup("nominal_sfp_r1") == Placement(
        scenario_id="pilot-v1/nominal_sfp", seed=1, scene_digest=scene_digest(TRIAL)
    )


def test_a_task_id_not_in_the_manifest_is_refused_with_the_fixed_message(tmp_path: Path) -> None:
    manifest = load_manifest(write(tmp_path / "m.json", manifest_data()))

    with pytest.raises(ManifestError) as refused:
        manifest.lookup(f"task_{SECRET}")

    assert str(refused.value) == TASK_NOT_IN_MANIFEST


FULL_TASK: dict[str, Any] = {
    "cable_type": "sfp_sc",
    "cable_name": "cable_0",
    "plug_type": "sfp",
    "plug_name": "sfp_tip",
    "port_type": "sfp",
    "port_name": "sfp_port_0",
    "target_module_name": "nic_card_mount_0",
    "time_limit": 180,
}


def _full_manifest(tmp_path: Path) -> Manifest:
    data = manifest_data()
    trial = {"scene": TRIAL["scene"], "task": FULL_TASK}
    data["scenes"]["nominal_sfp"] |= {"trial": trial, "digest": scene_digest(trial)}
    return load_manifest(write(tmp_path / "m.json", data))


def test_a_goal_whose_task_matches_its_scene_is_placed(tmp_path: Path) -> None:
    goal = {**FULL_TASK, "id": "nominal_sfp_r0", "time_limit": 180.0}  # a float in the core

    assert _full_manifest(tmp_path).lookup("nominal_sfp_r0", goal).seed == 0


@pytest.mark.parametrize(
    "change",
    [
        {"port_name": "sfp_port_1"},
        {"target_module_name": "nic_card_mount_1"},
        {"time_limit": 120.0},
        {"time_limit": True},
        {"cable_type": None},
    ],
)
def test_a_goal_whose_task_differs_from_its_scene_is_refused(
    tmp_path: Path, change: dict[str, object]
) -> None:
    goal = {**FULL_TASK, "id": "nominal_sfp_r0", **change}

    with pytest.raises(ManifestError) as refused:
        _full_manifest(tmp_path).lookup("nominal_sfp_r0", goal)

    assert str(refused.value) == TASK_MISMATCH


def test_a_scene_without_the_task_fields_refuses_every_goal_that_is_checked(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(write(tmp_path / "m.json", manifest_data()))  # a partial task

    with pytest.raises(ManifestError) as refused:
        manifest.lookup("nominal_sfp_r0", {**FULL_TASK, "id": "nominal_sfp_r0"})

    assert str(refused.value) == TASK_MISMATCH


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")
def test_a_fifo_is_refused_without_being_opened(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    os.mkfifo(path)  # no writer: opening it would block

    with pytest.raises(ManifestError) as refused:
        load_manifest(path)

    assert str(refused.value) == MANIFEST_UNREADABLE


def test_a_directory_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as refused:
        load_manifest(tmp_path)

    assert str(refused.value) == MANIFEST_UNREADABLE


def test_the_digest_ignores_key_order_and_follows_every_value() -> None:
    reordered = {"task": dict(reversed(TRIAL["task"].items())), "scene": TRIAL["scene"]}
    moved = {**TRIAL, "scene": {"task_board": {"pose": {"x": 0.16}}}}

    assert scene_digest(reordered) == scene_digest(TRIAL)
    assert scene_digest(moved) != scene_digest(TRIAL)


def test_a_missing_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as refused:
        load_manifest(tmp_path / "absent.json")

    assert str(refused.value) == MANIFEST_UNREADABLE


def test_a_file_over_the_size_bound_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_bytes(b" " * (MAX_BYTES + 1))

    with pytest.raises(ManifestError) as refused:
        load_manifest(path)

    assert str(refused.value) == MANIFEST_TOO_LARGE


def test_a_file_at_the_size_bound_is_read(tmp_path: Path) -> None:
    text = json.dumps(manifest_data())
    path = tmp_path / "m.json"
    path.write_text(text + " " * (MAX_BYTES - len(text.encode("utf-8"))), "utf-8")

    assert path.stat().st_size == MAX_BYTES
    assert load_manifest(path).name == "pilot-v1"


def _broken(change: str) -> dict[str, Any]:
    data = manifest_data()
    scene = data["scenes"]["nominal_sfp"]
    match change:
        case "not json":
            return {"raw": f"{{{SECRET}"}
        case "digest":
            scene["digest"] = "0" * 64
        case "extra field":
            scene["note"] = SECRET
        case "unknown scene":
            data["units"]["other_r0"] = {"scene": "other", "seed": 0}
        case "task id not scene and repeat":
            data["units"]["nominal_sfp_r7"] = {"scene": "nominal_sfp", "seed": 0}
        case "seed as text":
            data["units"]["nominal_sfp_r0"]["seed"] = "0"
        case "negative seed":
            data["units"]["nominal_sfp_r0"]["seed"] = -1
        case "bad task id":
            data["units"]["Nominal-SFP"] = {"scene": "nominal_sfp", "seed": 0}
        case "bad name":
            data["name"] = f"../{SECRET}"
        case "version":
            data["version"] = 2
        case "no units":
            data["units"] = {}
    return data


@pytest.mark.parametrize(
    "change",
    [
        "not json",
        "digest",
        "extra field",
        "unknown scene",
        "task id not scene and repeat",
        "seed as text",
        "negative seed",
        "bad task id",
        "bad name",
        "version",
        "no units",
    ],
)
def test_an_invalid_manifest_is_refused_without_repeating_its_content(
    tmp_path: Path, change: str
) -> None:
    data = _broken(change)
    path = tmp_path / "m.json"
    if change == "not json":
        path.write_text(data["raw"], "utf-8")
    else:
        write(path, data)

    with pytest.raises(ManifestError) as refused:
        load_manifest(path)

    assert str(refused.value) == MANIFEST_INVALID
    assert refused.value.__cause__ is None  # no parser text travels with it
    assert SECRET not in repr(refused.value)


def test_json_nested_too_deep_to_parse_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text("[" * 200_000 + "]" * 200_000, "utf-8")  # under the size bound

    with pytest.raises(ManifestError) as refused:
        load_manifest(path)

    assert str(refused.value) == MANIFEST_INVALID


def test_more_units_than_the_bound_are_refused(tmp_path: Path) -> None:
    data = manifest_data(repeats=MAX_UNITS + 1)

    with pytest.raises(ManifestError) as refused:
        load_manifest(write(tmp_path / "m.json", data))

    assert str(refused.value) in (MANIFEST_INVALID, MANIFEST_TOO_LARGE)


def test_units_up_to_the_bound_are_accepted(tmp_path: Path) -> None:
    data = manifest_data(repeats=MAX_UNITS)

    assert len(load_manifest(write(tmp_path / "m.json", data)).units) == MAX_UNITS
