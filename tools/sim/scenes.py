"""Engine configs for the AIC sim, written by us (ADR 026, ADR 027). Pure: YAML and JSON in and
out, nothing started.

A `Scene` is one trial of an aic_engine config without its task id: its `scene` node and its one
task. `engine_config` renders scenes into a config from the template that holds the engine's own
sections (`eval/sim/engine_template.yaml`), as trials `trial_1`... with task ids
`<scene id>_r<k>`, k the repeat (ADR 027). `build_manifest` writes the manifest the policy reads.

`validate_engine_config` mirrors the keys aic_engine requires before it starts a trial
(`Trial::Trial`, aic_engine.cpp:41-249; the root keys, :403-451), so a typo is refused here and
never stops an engine run at parse time on the VM (:414-424). It adds ADR 026's bounds:
- every rail translation inside the engine's effective clamp (`CLAMP_M`), so the engine never
  moves a value silently (:1826-1828, :1856-1858, :1888-1890);
- no trial with its own `task_board_limits`, so the clamp stays the engine's default (:1790-1812);
- the board flat, and every rail entity's roll and pitch at most 4° (`MAX_TILT_RAD`); the
  cable's pose in the gripper is not covered.
String fields must be strings (the engine's `.as<std::string>()` throws on a map or a list,
:240-247). A `task_board` key other than the pose and the rails below is refused: the engine's
`rail_<n>` ports (:140-177) are not mirrored here, so a config that uses them is refused rather
than passed unchecked.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from adapters.aic.manifest import TASK_FIELDS, Family, Manifest, SceneId, scene_digest

# aic_engine.cpp:1791-1796: the trial's own `task_board_limits` would replace these; the sample
# config's root-level block is never read.
CLAMP_M: dict[str, tuple[float, float]] = {
    "nic": (-0.048, 0.036),
    "sc": (-0.055, 0.055),
    "mount": (-0.09625, 0.09625),
}
MAX_TILT_RAD = math.radians(4.0)
MAX_CONFIG_BYTES = 1 << 20
NIC_RAILS = tuple(f"nic_rail_{i}" for i in range(5))
SC_RAILS = ("sc_rail_0", "sc_rail_1")
MOUNT_RAILS = (
    "lc_mount_rail_0",
    "sfp_mount_rail_0",
    "sc_mount_rail_0",
    "lc_mount_rail_1",
    "sfp_mount_rail_1",
    "sc_mount_rail_1",
)
BOARD_KEYS = frozenset({"pose", *NIC_RAILS, *SC_RAILS, *MOUNT_RAILS})
BOARD_POSE_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")
ENTITY_POSE_KEYS = ("translation", "roll", "pitch", "yaw")
CABLE_POSE_KEYS = ("gripper_offset", "roll", "pitch", "yaw")
TASK_KEYS = TASK_FIELDS  # the keys `Trial::Trial` reads in a task are the ones a scene fixes
_TILT_EPS = 1e-12


class EngineConfigError(ValueError):
    """An engine config, template or scene we would not hand to aic_engine. The message names
    the key path, which can hold a key read from the file; it never holds a value."""


class Scene(BaseModel):
    """One trial without its task id: `trial` holds its `scene` node and its `task`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: SceneId  # the manifest's own field types (adapters/aic/manifest.py)
    family: Family
    hypothesis: str = Field(default="", max_length=300)
    expected_class: Family = "unknown"
    trial: dict[str, Any]

    @property
    def digest(self) -> str:
        return scene_digest(self.trial)

    def task_id(self, repeat: int) -> str:
        return f"{self.scene_id}_r{repeat}"

    def render(self) -> dict[str, Any]:
        """The trial node of an engine config, before its task is named."""
        return {"scene": self.trial["scene"], "task": self.trial["task"]}


def scene_from_trial(scene_id: str, family: str, trial: Mapping[str, Any]) -> Scene:
    """The scene of an engine config's trial (for example the sample's `trial_1`), which must
    hold exactly one task; the task's id is dropped."""
    tasks = trial.get("tasks")
    if not isinstance(tasks, Mapping) or len(tasks) != 1:
        raise ValueError("a scene is a trial with exactly one task")
    (task,) = tasks.values()
    return Scene(
        scene_id=scene_id,
        family=family,
        trial={"scene": _plain(trial["scene"]), "task": _plain(task)},
    )


def load_template(path: Path) -> dict[str, Any]:
    """The engine sections every config starts from: `scoring` and `robot`, and no trials."""
    template = yaml.safe_load(read_bounded(path))
    if not isinstance(template, dict) or set(template) != {"scoring", "robot"}:
        raise EngineConfigError("template: must hold exactly the keys scoring and robot")
    return template


def engine_config(
    units: Sequence[tuple[Scene, int]], template: Mapping[str, Any]
) -> dict[str, Any]:
    """An engine config with one trial per (scene, repeat), in order, validated."""
    trials: dict[str, Any] = {}
    seen: set[str] = set()
    for n, (scene, repeat) in enumerate(units, start=1):
        task_id = scene.task_id(repeat)
        if task_id in seen:
            raise EngineConfigError(f"trials: the task id {task_id} appears twice")
        seen.add(task_id)
        node = scene.render()
        trials[f"trial_{n}"] = {"scene": node["scene"], "tasks": {task_id: node["task"]}}
    config = {**_plain(template), "trials": trials}
    validate_engine_config(config)
    return config


def build_manifest(
    name: str, scenes: Sequence[Scene], *, repeats: int | Mapping[str, int]
) -> dict[str, Any]:
    """The manifest (ADR 027) for `scenes`, each with repeats 0 to n - 1: n is `repeats`, or
    `repeats[scene_id]` when it is a map. The result is checked by the policy's own model
    (`Manifest`), so a manifest written here is one the policy loads."""
    ids = [s.scene_id for s in scenes]
    if len(set(ids)) != len(ids):
        raise ValueError("two scenes share a scene id")
    counts = {i: repeats if isinstance(repeats, int) else repeats[i] for i in ids}
    if any(n < 1 for n in counts.values()):
        raise ValueError("repeats must be at least 1")
    manifest = {
        "name": name,
        "version": 1,
        "scenes": {
            s.scene_id: {
                "family": s.family,
                "digest": s.digest,
                "hypothesis": s.hypothesis,
                "expected_class": s.expected_class,
                "trial": s.trial,
            }
            for s in scenes
        },
        "units": {
            s.task_id(k): {"scene": s.scene_id, "seed": k}
            for s in scenes
            for k in range(counts[s.scene_id])
        },
    }
    Manifest.model_validate(manifest)
    return manifest


def scenes_of(manifest: Mapping[str, Any]) -> dict[str, Scene]:
    """A manifest's scenes by id, as the runner renders them (ADR 028: an engine config is
    rendered from the manifest's own scene entries)."""
    return {
        scene_id: Scene(
            scene_id=scene_id,
            family=entry["family"],
            hypothesis=entry["hypothesis"],
            expected_class=entry["expected_class"],
            trial=entry["trial"],
        )
        for scene_id, entry in manifest["scenes"].items()
    }


def read_bounded(path: Path, limit: int = MAX_CONFIG_BYTES) -> str:
    """A config file's text, refused past `limit` bytes however the file behaves."""
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise EngineConfigError(f"{path.name}: over {limit} bytes")
    return raw.decode("utf-8")


def write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    """Keys in the order given and LF line endings, so the same config is the same file on every
    OS. Never sorted: aic_engine runs the trials in file order (aic_engine.cpp:410-416), and a
    sort would put trial_10 before trial_2."""
    text = yaml.safe_dump(_plain(data), sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8", newline="\n")


# --- validation ------------------------------------------------------------------------------


def validate_engine_config(config: object) -> None:
    """Raise EngineConfigError, naming the key path, for anything aic_engine would refuse or
    ADR 026 does not allow."""
    root = _map(config, "config")
    _get(root, "scoring", "")
    robot = _map(_get(root, "robot", ""), "robot")
    _get(robot, "home_joint_positions", "robot")
    trials = _map(_get(root, "trials", ""), "trials")
    if not trials:
        raise EngineConfigError("trials: must be a non-empty map")
    for trial_id, trial in trials.items():
        _trial(f"trials.{trial_id}", trial)


def _trial(where: str, trial: object) -> None:
    node = _map(trial, where)
    if "task_board_limits" in node:
        raise EngineConfigError(
            f"{where}.task_board_limits: refused, so the rail clamp stays the engine's default"
        )
    scene = _map(_get(node, "scene", where), f"{where}.scene")
    tasks = _map(_get(node, "tasks", where), f"{where}.tasks")
    board_at = f"{where}.scene.task_board"
    board = _map(_get(scene, "task_board", f"{where}.scene"), board_at)
    unknown = sorted(str(k) for k in board if k not in BOARD_KEYS)
    if unknown:
        raise EngineConfigError(f"{board_at}.{unknown[0]}: not a key this validator mirrors")
    pose = _map(_get(board, "pose", board_at), f"{board_at}.pose")
    for key in BOARD_POSE_KEYS:
        _number(pose, key, f"{board_at}.pose")
    for key in ("roll", "pitch"):
        if pose[key] != 0:
            raise EngineConfigError(f"{board_at}.pose.{key}: the board must be flat")
    for rail in NIC_RAILS:
        _rail(board, rail, "nic", board_at, required=True)
    for rail in SC_RAILS:
        _rail(board, rail, "sc", board_at, required=True)
    for rail in MOUNT_RAILS:
        _rail(board, rail, "mount", board_at, required=False)
    cables_at = f"{where}.scene.cables"
    cables = _map(_get(scene, "cables", f"{where}.scene"), cables_at)
    if not cables:
        raise EngineConfigError(f"{cables_at}: must be a non-empty map")
    for cable_id, cable in cables.items():
        _cable(f"{cables_at}.{cable_id}", cable)
    if not tasks:
        raise EngineConfigError(f"{where}.tasks: must be a non-empty map")
    for task_id, task in tasks.items():
        _task(f"{where}.tasks.{task_id}", task)


def _rail(board: Mapping[str, Any], key: str, kind: str, board_at: str, *, required: bool) -> None:
    where = f"{board_at}.{key}"
    if key not in board:
        if required:
            raise EngineConfigError(f"{where}: missing")
        return
    rail = _map(board[key], where)
    present = _get(rail, "entity_present", where)
    if not isinstance(present, bool):
        raise EngineConfigError(f"{where}.entity_present: must be true or false")
    if not present:
        return
    _string(rail, "entity_name", where)
    pose_at = f"{where}.entity_pose"
    pose = _map(_get(rail, "entity_pose", where), pose_at)
    for name in ENTITY_POSE_KEYS:
        _number(pose, name, pose_at)
    lo, hi = CLAMP_M[kind]
    if not lo <= pose["translation"] <= hi:
        raise EngineConfigError(
            f"{pose_at}.translation: outside the engine's clamp [{lo}, {hi}] for a {kind} rail"
        )
    for name in ("roll", "pitch"):
        if abs(pose[name]) > MAX_TILT_RAD + _TILT_EPS:
            raise EngineConfigError(f"{pose_at}.{name}: a tilt over 4 degrees")


def _cable(where: str, cable: object) -> None:
    node = _map(cable, where)
    pose = _map(_get(node, "pose", where), f"{where}.pose")
    for key in CABLE_POSE_KEYS:
        _get(pose, key, f"{where}.pose")
    for key in ("roll", "pitch", "yaw"):
        _number(pose, key, f"{where}.pose")
    offset_at = f"{where}.pose.gripper_offset"
    offset = _map(pose["gripper_offset"], offset_at)
    for key in ("x", "y", "z"):
        _number(offset, key, offset_at)
    if not isinstance(_get(node, "attach_cable_to_gripper", where), bool):
        raise EngineConfigError(f"{where}.attach_cable_to_gripper: must be true or false")
    _string(node, "cable_type", where)


def _task(where: str, task: object) -> None:
    node = _map(task, where)
    for key in TASK_KEYS:
        if key != "time_limit":
            _string(node, key, where)
    limit = _get(node, "time_limit", where)
    if type(limit) is not int or limit <= 0:
        raise EngineConfigError(f"{where}.time_limit: must be a positive whole number")


def _map(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EngineConfigError(f"{where}: must be a map")
    return value


def _get(node: Mapping[str, Any], key: str, where: str) -> Any:
    path = f"{where}.{key}" if where else key
    if key not in node or node[key] is None:
        raise EngineConfigError(f"{path}: missing")
    return node[key]


def _string(node: Mapping[str, Any], key: str, where: str) -> str:
    value = _get(node, key, where)
    if not isinstance(value, str) or not value:
        raise EngineConfigError(f"{where}.{key}: must be a non-empty string")
    return value


def _number(node: Mapping[str, Any], key: str, where: str) -> float:
    value = _get(node, key, where)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise EngineConfigError(f"{where}.{key}: must be a finite number")
    return float(value)


def _plain(value: Any) -> Any:
    """A deep copy made of dicts, lists and scalars only, with string keys."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value
