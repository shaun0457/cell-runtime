"""Engine configs we write (`tools/sim/scenes.py`, ADR 026, ADR 027).

`fixtures/sim/sample_config.yaml` is aic_engine's sample config, copied unchanged from
intrinsic-dev/aic @ 3ab0653 (`aic_engine/config/sample_config.yaml`, Apache-2.0). The validator
must accept it, and refuse a config with any key that `Trial::Trial` or the engine's start
requires (aic_engine.cpp:41-249, :403-451), so that a typo is caught here and never stops an
engine run at parse time on the VM.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from adapters.aic.manifest import Manifest
from tools.sim.scenes import (
    CLAMP_M,
    MAX_CONFIG_BYTES,
    MAX_TILT_RAD,
    EngineConfigError,
    Scene,
    build_manifest,
    engine_config,
    load_template,
    read_bounded,
    scene_from_trial,
    scenes_of,
    validate_engine_config,
    write_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
REPO = Path(__file__).parent.parent
TEMPLATE = REPO / "eval" / "sim" / "engine_template.yaml"


def _sample() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "sample_config.yaml").read_text("utf-8"))


def test_a_written_config_keeps_its_trials_in_run_order(tmp_path: Path) -> None:
    # aic_engine runs the trials in file order (aic_engine.cpp:410-416): sorted keys would put
    # trial_10 before trial_2 and run the units out of the order the runner charges them in.
    scene = scene_from_trial("nominal_sfp", "nominal", _sample()["trials"]["trial_1"])
    path = tmp_path / "engine.yaml"

    write_yaml(path, engine_config([(scene, k) for k in range(11)], load_template(TEMPLATE)))

    trials = yaml.safe_load(path.read_text("utf-8"))["trials"]
    assert list(trials) == [f"trial_{n}" for n in range(1, 12)]
    assert [next(iter(t["tasks"])) for t in trials.values()] == [
        f"nominal_sfp_r{k}" for k in range(11)
    ]
    assert b"\r\n" not in path.read_bytes()  # LF on every OS


def _drop(config: dict[str, Any], path: str) -> None:
    node: Any = config
    *parents, last = path.split(".")
    for key in parents:
        node = node[key]
    del node[last]


def _set(config: dict[str, Any], path: str, value: object) -> None:
    node: Any = config
    *parents, last = path.split(".")
    for key in parents:
        node = node[key]
    node[last] = value


def test_the_sample_config_is_accepted() -> None:
    validate_engine_config(_sample())


T1 = "trials.trial_1"
BOARD = f"{T1}.scene.task_board"
CABLE = f"{T1}.scene.cables.cable_0"
REQUIRED = [
    "scoring",
    "robot",
    "robot.home_joint_positions",
    "trials",
    f"{T1}.scene",
    f"{T1}.tasks",
    BOARD,
    f"{BOARD}.pose",
    *(f"{BOARD}.pose.{k}" for k in ("x", "y", "z", "roll", "pitch", "yaw")),
    *(f"{BOARD}.nic_rail_{i}" for i in range(5)),
    *(f"{BOARD}.nic_rail_{i}.entity_present" for i in range(5)),
    f"{BOARD}.nic_rail_0.entity_name",
    f"{BOARD}.nic_rail_0.entity_pose",
    *(f"{BOARD}.nic_rail_0.entity_pose.{k}" for k in ("translation", "roll", "pitch", "yaw")),
    f"{BOARD}.sc_rail_0",
    f"{BOARD}.sc_rail_1",
    f"{BOARD}.sc_rail_0.entity_present",
    f"{BOARD}.sc_rail_0.entity_name",
    f"{BOARD}.sc_rail_0.entity_pose",
    *(f"{BOARD}.sc_rail_0.entity_pose.{k}" for k in ("translation", "roll", "pitch", "yaw")),
    f"{T1}.scene.cables",
    f"{CABLE}.pose",
    *(f"{CABLE}.pose.{k}" for k in ("gripper_offset", "roll", "pitch", "yaw")),
    *(f"{CABLE}.pose.gripper_offset.{k}" for k in ("x", "y", "z")),
    f"{CABLE}.attach_cable_to_gripper",
    f"{CABLE}.cable_type",
    *(
        f"{T1}.tasks.task_1.{k}"
        for k in (
            "cable_type",
            "cable_name",
            "plug_type",
            "plug_name",
            "port_type",
            "port_name",
            "target_module_name",
            "time_limit",
        )
    ),
]


@pytest.mark.parametrize("path", REQUIRED)
def test_a_config_without_a_required_key_is_refused_and_named(path: str) -> None:
    config = _sample()
    _drop(config, path)

    with pytest.raises(EngineConfigError) as refused:
        validate_engine_config(config)

    assert path in str(refused.value)


STRINGS = [
    f"{BOARD}.nic_rail_0.entity_name",
    f"{CABLE}.cable_type",
    *(f"{T1}.tasks.task_1.{k}" for k in ("cable_name", "port_name", "target_module_name")),
]


@pytest.mark.parametrize("path", STRINGS)
@pytest.mark.parametrize("value", [{"nested": "map"}, ["a", "list"], 7, ""])
def test_a_string_field_that_is_not_a_string_is_refused_and_named(path: str, value: object) -> None:
    config = _sample()
    _set(config, path, value)

    with pytest.raises(EngineConfigError, match="non-empty string") as refused:
        validate_engine_config(config)

    assert path in str(refused.value)


def test_a_task_board_key_the_validator_does_not_mirror_is_refused() -> None:
    config = _sample()
    _set(config, f"{BOARD}.rail_0", {"ports": {"port_0": {"type": "sfp"}}})

    with pytest.raises(EngineConfigError, match=r"rail_0: not a key"):
        validate_engine_config(config)


@pytest.mark.parametrize(
    ("rail", "kind"),
    [("nic_rail_0", "nic"), ("sc_rail_0", "sc"), ("sfp_mount_rail_0", "mount")],
)
def test_a_translation_at_the_clamp_ends_is_accepted_and_past_them_refused(
    rail: str, kind: str
) -> None:
    lo, hi = CLAMP_M[kind]
    path = f"{BOARD}.{rail}.entity_pose.translation"
    for value in (lo, hi):
        config = _sample()
        _set(config, path, value)
        validate_engine_config(config)
    for value in (lo - 1e-4, hi + 1e-4):
        config = _sample()
        _set(config, path, value)
        with pytest.raises(EngineConfigError, match="clamp"):
            validate_engine_config(config)


def test_the_clamp_is_the_engines_hard_coded_default() -> None:
    # aic_engine.cpp:1791-1796; the root-level task_board_limits block is never read (:1790).
    assert CLAMP_M == {
        "nic": (-0.048, 0.036),
        "sc": (-0.055, 0.055),
        "mount": (-0.09625, 0.09625),
    }


@pytest.mark.parametrize("axis", ["roll", "pitch"])
def test_an_entity_tilted_up_to_4_degrees_is_accepted_and_beyond_refused(axis: str) -> None:
    assert math.isclose(MAX_TILT_RAD, math.radians(4.0))
    path = f"{BOARD}.nic_rail_0.entity_pose.{axis}"
    config = _sample()
    _set(config, path, -MAX_TILT_RAD)
    validate_engine_config(config)
    config = _sample()
    _set(config, path, math.radians(4.01))
    with pytest.raises(EngineConfigError, match="tilt"):
        validate_engine_config(config)


@pytest.mark.parametrize("axis", ["roll", "pitch"])
def test_a_tilted_board_is_refused(axis: str) -> None:
    config = _sample()
    _set(config, f"{BOARD}.pose.{axis}", 0.01)

    with pytest.raises(EngineConfigError, match="flat"):
        validate_engine_config(config)


def test_a_trial_with_its_own_board_limits_is_refused() -> None:
    config = _sample()
    config["trials"]["trial_1"]["task_board_limits"] = config["task_board_limits"]

    with pytest.raises(EngineConfigError, match="task_board_limits"):
        validate_engine_config(config)


@pytest.mark.parametrize("path", ["trials", f"{T1}.tasks", f"{T1}.scene.cables"])
def test_an_empty_map_is_refused(path: str) -> None:
    config = _sample()
    _set(config, path, {})

    with pytest.raises(EngineConfigError, match=path.replace(".", r"\.")):
        validate_engine_config(config)


@pytest.mark.parametrize("value", [0, -5, 1.5, True, "180"])
def test_a_time_limit_that_is_not_a_positive_whole_number_is_refused(value: object) -> None:
    config = _sample()
    _set(config, f"{T1}.tasks.task_1.time_limit", value)

    with pytest.raises(EngineConfigError, match="time_limit"):
        validate_engine_config(config)


def test_a_number_that_is_a_bool_or_text_is_refused() -> None:
    for value in (True, "0.15"):
        config = _sample()
        _set(config, f"{BOARD}.pose.x", value)
        with pytest.raises(EngineConfigError, match=r"pose\.x"):
            validate_engine_config(config)


# --- scenes and the configs rendered from them -----------------------------------------------


def _nominal() -> Scene:
    return scene_from_trial("nominal_sfp", "nominal", _sample()["trials"]["trial_1"])


def test_a_scene_keeps_the_trial_without_its_task_id() -> None:
    scene = _nominal()

    assert set(scene.trial) == {"scene", "task"}
    assert scene.trial["task"]["port_name"] == "sfp_port_0"
    assert len(scene.digest) == 64


def test_a_trial_with_more_than_one_task_is_not_a_scene() -> None:
    trial = copy.deepcopy(_sample()["trials"]["trial_1"])
    trial["tasks"]["task_2"] = trial["tasks"]["task_1"]

    with pytest.raises(ValueError, match="one task"):
        scene_from_trial("two_tasks", "nominal", trial)


def test_the_digest_follows_the_scene_and_not_its_name() -> None:
    a = _nominal()
    b = scene_from_trial("renamed", "other", _sample()["trials"]["trial_1"])
    moved = copy.deepcopy(_sample()["trials"]["trial_1"])
    moved["scene"]["task_board"]["pose"]["x"] = 0.16
    c = scene_from_trial("nominal_sfp", "nominal", moved)

    assert a.digest == b.digest
    assert a.digest != c.digest


def test_the_engine_config_names_each_task_by_scene_and_repeat() -> None:
    template = load_template(TEMPLATE)
    scene = _nominal()

    config = engine_config([(scene, 0), (scene, 1)], template)

    validate_engine_config(config)
    assert list(config["trials"]) == ["trial_1", "trial_2"]
    assert [next(iter(t["tasks"])) for t in config["trials"].values()] == [
        "nominal_sfp_r0",
        "nominal_sfp_r1",
    ]
    assert config["scoring"] == template["scoring"]
    assert config["robot"] == template["robot"]
    assert "task_board_limits" not in config


def test_an_engine_config_with_a_task_id_twice_is_refused() -> None:
    scene = _nominal()

    with pytest.raises(EngineConfigError, match="nominal_sfp_r0"):
        engine_config([(scene, 0), (scene, 0)], load_template(TEMPLATE))


def test_the_template_holds_the_samples_engine_sections_and_no_trials() -> None:
    template = load_template(TEMPLATE)
    sample = _sample()

    assert template["scoring"] == sample["scoring"]
    assert template["robot"] == sample["robot"]
    assert set(template) == {"scoring", "robot"}


def test_a_template_with_trials_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "template.yaml"
    path.write_text(yaml.safe_dump(_sample()), "utf-8")

    with pytest.raises(EngineConfigError, match="template"):
        load_template(path)


def _sc() -> Scene:
    return scene_from_trial("nominal_sc", "nominal", _sample()["trials"]["trial_3"])


def test_a_manifest_can_give_each_scene_its_own_repeats() -> None:
    manifest = build_manifest(
        "pilot-v1", [_nominal(), _sc()], repeats={"nominal_sfp": 2, "nominal_sc": 1}
    )

    assert list(manifest["units"]) == ["nominal_sfp_r0", "nominal_sfp_r1", "nominal_sc_r0"]
    assert Manifest.model_validate(manifest).name == "pilot-v1"


@pytest.mark.parametrize("repeats", [0, {"nominal_sfp": 1, "nominal_sc": 0}])
def test_a_manifest_with_a_scene_of_no_repeats_is_refused(repeats: object) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        build_manifest("pilot-v1", [_nominal(), _sc()], repeats=repeats)  # type: ignore[arg-type]


def test_a_manifest_with_two_scenes_of_one_id_is_refused() -> None:
    other = scene_from_trial("nominal_sfp", "nominal", _sample()["trials"]["trial_3"])

    with pytest.raises(ValueError, match="share a scene id"):
        build_manifest("pilot-v1", [_nominal(), other], repeats=1)


def test_a_manifest_the_policy_would_refuse_is_never_built() -> None:
    with pytest.raises(ValueError):  # the name breaks the policy's pattern
        build_manifest("Pilot V1", [_nominal()], repeats=1)


def test_the_scenes_of_a_manifest_are_the_ones_it_was_built_from() -> None:
    scenes = [_nominal(), _sc()]

    rebuilt = scenes_of(build_manifest("pilot-v1", scenes, repeats=1))

    assert rebuilt == {s.scene_id: s for s in scenes}


def test_the_scenes_of_a_manifest_take_the_models_defaults() -> None:
    manifest = build_manifest("pilot-v1", [_nominal()], repeats=1)
    for key in ("hypothesis", "expected_class"):
        del manifest["scenes"]["nominal_sfp"][key]

    (scene,) = scenes_of(manifest).values()

    assert (scene.hypothesis, scene.expected_class) == ("", "unknown")


def test_the_scenes_of_a_manifest_with_a_wrong_digest_are_refused() -> None:
    manifest = build_manifest("pilot-v1", [_nominal()], repeats=1)
    manifest["scenes"]["nominal_sfp"]["digest"] = "0" * 64

    with pytest.raises(ValueError):
        scenes_of(manifest)


@pytest.mark.parametrize(
    "repeats", [{"nominal_sfp": 1}, {"nominal_sfp": 1, "nominal_sc": 1, "other": 1}]
)
def test_repeats_by_scene_must_name_exactly_the_scenes(repeats: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="exactly"):
        build_manifest("pilot-v1", [_nominal(), _sc()], repeats=repeats)


def test_a_file_is_read_up_to_its_bound_and_refused_past_it(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"#" * 16)

    assert read_bounded(path, limit=16) == "#" * 16
    with pytest.raises(EngineConfigError, match="over 15 bytes"):
        read_bounded(path, limit=15)


def test_a_config_over_the_default_bound_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))

    with pytest.raises(EngineConfigError, match=f"over {MAX_CONFIG_BYTES} bytes"):
        read_bounded(path)
