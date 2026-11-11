"""Did a scene's perturbation take effect? (ADR 026, #82.) Pure: no ROS, no files.

`world_ready` (#79) records the poses the oracle read once the world was built, in the robot's
base frame: the target port, the plug and the gripper's TCP. A scene is checked against the
reference of its port type, the `nominal` scene (the sample's trial, unchanged): the one value
the scene's request changes from the reference's is read back in the frame the request sets it
in, so a board pose drawn alongside it does not count as a change.

What the request changes, what is read back (scene against reference), and the tolerance:
- the target entity's roll or pitch (tilt): the angle the port turned, in the board's frame;
  0.25°;
- the target entity's yaw: the same angle; 0.5°;
- the target entity's translation: the distance the port moved, in the board's frame; 1 mm;
- the cable's `gripper_offset`: the distance the plug moved in the gripper's frame, 1 mm; and
  the angle it turned there, which must be at most 0.02 rad;
- one of the cable's roll, pitch or yaw: the angle the plug turned in the gripper's frame;
  0.02 rad.

Why each reads back as the request, with no model of the board's insides:
- aic_engine spawns the board at its pose in `world` (aic_engine.cpp:1173-1179, :1958), and the
  robot's base sits in `world` at the launch defaults (`ROBOT_BASE_IN_WORLD`), so a base-frame
  pose moves into the board's frame exactly.
- Changing one angle of a roll-pitch-yaw triple by d turns everything fixed after it by d,
  whatever the other two angles and whatever fixed transforms the xacro puts before and after
  it: the relative rotation is a conjugate of a turn by d about one axis. A translation along a
  rail moves the port by its own length, since the entity's rotation is unchanged.
- The cable is spawned at the gripper's position plus `gripper_offset` in `world`'s axes, turned
  by its roll, pitch and yaw in `world` (aic_engine.cpp:1229-1239), with the arm at its home in
  every trial. So the plug's pose in the gripper's frame changes by the request. A turn of the
  cable also moves the plug, by an amount that depends on where the plug sits on the cable, so
  only its angle is checked.

Only scenes of `CHECKED_FAMILIES` are checked, and only when their request changes exactly one
of the values above from the reference's; the board pose itself, `inrange` draws, `empty_rail`
and `reversed` are recorded, not checked (ADR 026).

`spread` reads a port type's other nominal trials against its reference in the same frames: how
far trials of one unchanged request read apart, which a tolerance must be wider than for its
check to mean anything.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cellrt.runtime.events import PoseRecord, WorldReady

# The tabletop joint's launch defaults: x, y, z, roll, pitch, yaw of the robot's base in `world`
# (aic_gz_bringup.launch.py:578-608; ur_gz.urdf.xacro:62-66, the robot at its origin, :106).
ROBOT_BASE_IN_WORLD = (-0.2, 0.2, 1.14, 0.0, 0.0, -3.141)

TILT, YAW, SHIFT, GRASP_SHIFT, GRASP_TURN = "tilt", "yaw", "shift", "grasp shift", "grasp turn"
TOLERANCES = {
    TILT: math.radians(0.25),
    YAW: math.radians(0.5),
    SHIFT: 0.001,
    GRASP_SHIFT: 0.001,
    GRASP_TURN: 0.02,
}
GRASP_SHIFT_TURN_TOLERANCE = 0.02  # rad: a grasp shift's plug must not turn
# ADR 026: the families whose scenes change one value; the others are recorded, not checked.
CHECKED_FAMILIES = ("tilt", "yaw_edge", "trans_edge", "beyond")

_RAIL_OF_MODULE = {"nic_card_mount": "nic_rail", "sc_port": "sc_rail"}  # aic_engine.cpp:1814-1872
_ENTITY_KEYS = frozenset({"translation", "roll", "pitch", "yaw"})
_OFFSET_KEYS = frozenset({"x", "y", "z"})
_ANGLE_KEYS = frozenset({"roll", "pitch", "yaw"})
_MIN_NORM = 1e-9

Vec = tuple[float, float, float]
Quat = tuple[float, float, float, float]  # w, x, y, z


@dataclass(frozen=True)
class Change:
    """The one value a scene's request changes from its reference's, and by how much (rad or
    m)."""

    kind: str
    size: float

    @property
    def tolerance(self) -> float:
        return TOLERANCES[self.kind]


@dataclass(frozen=True)
class Reading:
    """One trial's readback of its scene's change, against the reference's readback."""

    change: Change
    measured: float
    turn: float | None = None  # a grasp shift's: how far the plug turned, which must be none

    @property
    def badness(self) -> float:
        """How far off, in tolerances: above 1 is not applied."""
        off = abs(self.measured - self.change.size) / self.change.tolerance
        if self.turn is not None:
            off = max(off, self.turn / GRASP_SHIFT_TURN_TOLERANCE)
        return off

    @property
    def applied(self) -> bool:
        return self.badness <= 1.0


def checked_family(family: str) -> bool:
    return any(family == f or family.startswith(f + "_") for f in CHECKED_FAMILIES)


def requested_change(scene: Mapping[str, Any], reference: Mapping[str, Any]) -> Change | None:
    """The one value `scene`'s trial changes from `reference`'s, the board's pose aside; None
    when it changes none, more than one, or one this module does not read back, or when either
    trial is not shaped as aic_engine reads it."""
    try:
        return _change(scene, reference)
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def read_back(
    change: Change,
    scene: Mapping[str, Any],
    ready: WorldReady,
    reference: Mapping[str, Any],
    reference_ready: WorldReady,
) -> Reading | None:
    """The scene's readback of `change` against the reference's; None when a pose it needs was
    not read in either run, or a board pose is not one aic_engine reads."""
    try:
        if change.kind in (TILT, YAW, SHIFT):
            a = _port_in_board(scene, ready.port)
            b = _port_in_board(reference, reference_ready.port)
        else:
            a, b = _plug_in_gripper(ready), _plug_in_gripper(reference_ready)
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    if a is None or b is None:
        return None
    if change.kind == SHIFT:
        return Reading(change, _distance(a[0], b[0]))
    if change.kind == GRASP_SHIFT:
        return Reading(change, _distance(a[0], b[0]), turn=_turn(a[1], b[1]))
    return Reading(change, _turn(a[1], b[1]))


@dataclass(frozen=True)
class Spread:
    """How far a port type's other nominal trials read from its reference, at most (rad, m):
    the port's turn and shift in the board's frame, the plug's shift and turn in the gripper's,
    over the `trials` that read every pose."""

    trials: int
    port_turn: float
    port_shift: float
    plug_shift: float
    plug_turn: float

    @property
    def wider_than_a_tolerance(self) -> bool:
        return (
            self.port_turn > TOLERANCES[TILT]
            or self.port_shift > TOLERANCES[SHIFT]
            or self.plug_shift > TOLERANCES[GRASP_SHIFT]
            or self.plug_turn > TOLERANCES[GRASP_TURN]
        )


def spread(
    reference: Mapping[str, Any],
    reference_ready: WorldReady,
    others: Iterable[tuple[Mapping[str, Any], WorldReady]],
) -> Spread | None:
    """The spread of `others` (each a nominal trial's request and readback) about the reference;
    None when the reference's own readback gives no reading. An other that gives none is
    skipped."""
    ref = _nominal_reading(reference, reference_ready)
    if ref is None:
        return None
    trials, worst = 0, [0.0, 0.0, 0.0, 0.0]
    for trial, ready in others:
        seen = _nominal_reading(trial, ready)
        if seen is None:
            continue
        (port, plug), (port_ref, plug_ref) = seen, ref
        off = (
            _turn(port[1], port_ref[1]),
            _distance(port[0], port_ref[0]),
            _distance(plug[0], plug_ref[0]),
            _turn(plug[1], plug_ref[1]),
        )
        trials += 1
        worst = [max(w, o) for w, o in zip(worst, off, strict=True)]
    return Spread(trials, *worst)


def reads_back(trial: Mapping[str, Any], ready: WorldReady) -> bool:
    """Whether a trial's readback gives both the port's pose in the board's frame and the
    plug's in the gripper's: what a reference must give."""
    return _nominal_reading(trial, ready) is not None


def _nominal_reading(trial: Mapping[str, Any], ready: WorldReady) -> tuple[Pose, Pose] | None:
    try:
        port, plug = _port_in_board(trial, ready.port), _plug_in_gripper(ready)
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    return None if port is None or plug is None else (port, plug)


# --- the request ---------------------------------------------------------------------------


def _change(scene: Mapping[str, Any], reference: Mapping[str, Any]) -> Change | None:
    if scene["task"] != reference["task"]:
        return None
    s, r = scene["scene"], reference["scene"]
    if set(s) != set(r) or any(s[k] != r[k] for k in s if k not in ("task_board", "cables")):
        return None
    rail = _target_rail(scene["task"]["target_module_name"])
    board_s, board_r = s["task_board"], r["task_board"]
    if set(board_s) != set(board_r):
        return None
    if any(board_s[k] != board_r[k] for k in board_s if k not in ("pose", rail)):
        return None
    if _without(board_s[rail], "entity_pose") != _without(board_r[rail], "entity_pose"):
        return None
    entity = _diff(board_s[rail]["entity_pose"], board_r[rail]["entity_pose"], _ENTITY_KEYS)
    cables_s, cables_r = s["cables"], r["cables"]
    if set(cables_s) != set(cables_r) or len(cables_s) != 1:
        return None
    (name,) = cables_s
    cable_s, cable_r = cables_s[name], cables_r[name]
    if _without(cable_s, "pose") != _without(cable_r, "pose"):
        return None
    pose_s, pose_r = cable_s["pose"], cable_r["pose"]
    if set(pose_s) != {"gripper_offset", *_ANGLE_KEYS} or set(pose_r) != set(pose_s):
        return None
    offset = _diff(pose_s["gripper_offset"], pose_r["gripper_offset"], _OFFSET_KEYS)
    angles = _diff(
        _without(pose_s, "gripper_offset"), _without(pose_r, "gripper_offset"), _ANGLE_KEYS
    )
    changed = [d for d in (entity, offset, angles) if d]
    if len(changed) != 1:
        return None
    if offset:
        return Change(GRASP_SHIFT, math.hypot(*offset.values()))
    (diff,) = changed
    if len(diff) != 1:
        return None
    ((key, delta),) = diff.items()
    if angles:
        return Change(GRASP_TURN, abs(delta))
    return Change({"translation": SHIFT, "yaw": YAW}.get(key, TILT), abs(delta))


def _target_rail(module: str) -> str:
    """`nic_card_mount_2` sits on `nic_rail_2`, `sc_port_1` on `sc_rail_1`."""
    kind, _, index = module.rpartition("_")
    if not index.isdigit():
        raise ValueError("not a module on a rail")
    return f"{_RAIL_OF_MODULE[kind]}_{index}"


def _without(node: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k != key}


def _diff(a: Mapping[str, Any], b: Mapping[str, Any], keys: frozenset[str]) -> dict[str, float]:
    if set(a) != keys or set(b) != keys:
        raise ValueError("not the keys aic_engine reads")
    out = {}
    for key in sorted(keys):
        x, y = _number(a[key]), _number(b[key])
        if x != y:
            out[key] = x - y
    return out


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("not a number")
    try:
        number = float(value)
    except OverflowError:  # an int past a float's range
        raise ValueError("not a finite number") from None
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number


# --- the readback --------------------------------------------------------------------------

Pose = tuple[Vec, Quat]


def _port_in_board(trial: Mapping[str, Any], port: PoseRecord | None) -> Pose | None:
    """The port's pose in the board's frame: board⁻¹ · world←base · base←port."""
    seen = _pose(port)
    if seen is None:
        return None
    board = trial["scene"]["task_board"]["pose"]
    in_world = _rpy_pose(*(_number(board[k]) for k in ("x", "y", "z", "roll", "pitch", "yaw")))
    return _compose(_inverse(in_world), _compose(_rpy_pose(*ROBOT_BASE_IN_WORLD), seen))


def _plug_in_gripper(ready: WorldReady) -> Pose | None:
    plug, gripper = _pose(ready.plug), _pose(ready.gripper)
    if plug is None or gripper is None:
        return None
    return _compose(_inverse(gripper), plug)


def _pose(record: PoseRecord | None) -> Pose | None:
    """A recorded pose with its quaternion made unit; None if none was read."""
    if record is None:
        return None
    w, x, y, z = record.orientation
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm < _MIN_NORM:
        return None
    return record.position, (w / norm, x / norm, y / norm, z / norm)


def _rpy_pose(x: float, y: float, z: float, roll: float, pitch: float, yaw: float) -> Pose:
    """A pose from aic_engine's roll-pitch-yaw, converted as it converts them
    (aic_engine.cpp:1938-1949)."""
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    q = (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )
    return (x, y, z), q


def _compose(a: Pose, b: Pose) -> Pose:
    (pa, qa), (pb, qb) = a, b
    moved = _rotate(qa, pb)
    return (pa[0] + moved[0], pa[1] + moved[1], pa[2] + moved[2]), _mul(qa, qb)


def _inverse(a: Pose) -> Pose:
    p, q = a
    back = _conj(q)
    r = _rotate(back, p)
    return (-r[0], -r[1], -r[2]), back


def _mul(a: Quat, b: Quat) -> Quat:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _conj(q: Quat) -> Quat:
    return q[0], -q[1], -q[2], -q[3]


def _rotate(q: Quat, v: Vec) -> Vec:
    _, x, y, z = _mul(_mul(q, (0.0, *v)), _conj(q))
    return x, y, z


def _turn(a: Quat, b: Quat) -> float:
    """The angle of the rotation from b to a, in [0, π]."""
    w, x, y, z = _mul(a, _conj(b))
    return 2.0 * math.atan2(math.sqrt(x * x + y * y + z * z), abs(w))


def _distance(a: Vec, b: Vec) -> float:
    return math.dist(a, b)
