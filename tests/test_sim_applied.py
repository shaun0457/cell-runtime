"""Whether a scene's perturbation took effect (`tools/sim/applied.py`, ADR 026).

The readbacks come from a spawn written apart from the module, with rotation matrices instead
of quaternions: the board at its pose in `world`; inside it a rail frame the test makes up; the
entity's translation along the rail, then its roll-pitch-yaw; a port frame on the entity, made
up too. The cable sits at the gripper's position plus its offset, turned in `world`, with a
plug frame on it. The made-up frames stand for the xacro's insides, which the check must not
need."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from cellrt.runtime.events import PoseRecord, WorldReady
from tools.sim import applied
from tools.sim.applied import (
    GRASP_SHIFT,
    GRASP_TURN,
    SHIFT,
    TILT,
    YAW,
    Change,
    checked_family,
    read_back,
    requested_change,
    spread,
)
from tools.sim.scenes import scene_from_trial

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
SAMPLE = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text("utf-8"))
BASE = scene_from_trial("base", "base", SAMPLE["trials"]["trial_1"]).trial  # nic_rail_0, SFP
SC_BASE = scene_from_trial("sc", "sc", SAMPLE["trials"]["trial_3"]).trial  # sc_rail_1, cable_1

Matrix = list[list[float]]
Vec = tuple[float, float, float]
Frame = tuple[Matrix, Vec]


def _rx(a: float) -> Matrix:
    c, s = math.cos(a), math.sin(a)
    return [[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]]


def _ry(a: float) -> Matrix:
    c, s = math.cos(a), math.sin(a)
    return [[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]]


def _rz(a: float) -> Matrix:
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def _mm(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mv(a: Matrix, v: Vec) -> Vec:
    x, y, z = (sum(a[i][k] * v[k] for k in range(3)) for i in range(3))
    return x, y, z


def _rpy(roll: float, pitch: float, yaw: float) -> Matrix:
    return _mm(_rz(yaw), _mm(_ry(pitch), _rx(roll)))


def _then(a: Frame, b: Frame) -> Frame:
    (ra, ta), (rb, tb) = a, b
    moved = _mv(ra, tb)
    return _mm(ra, rb), (ta[0] + moved[0], ta[1] + moved[1], ta[2] + moved[2])


def _inv(a: Frame) -> Frame:
    r, t = a
    back = [[r[j][i] for j in range(3)] for i in range(3)]
    x, y, z = _mv(back, t)
    return back, (-x, -y, -z)


def _quat(m: Matrix) -> tuple[float, float, float, float]:
    """A rotation matrix's unit quaternion (w, x, y, z), by Shepperd's method."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        return 0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        return (m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        return (m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
    return (m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s


def _record(frame: Frame) -> PoseRecord:
    return PoseRecord(position=frame[1], orientation=_quat(frame[0]))


ROBOT: Frame = (_rz(-3.141), (-0.2, 0.2, 1.14))
RAIL: Frame = (_rpy(0.3, -0.2, 1.1), (0.10, -0.05, 0.02))  # the rail's frame in the board's
PORT: Frame = (_rpy(-0.5, 0.4, 0.7), (0.01, 0.02, 0.03))  # the port's frame on the entity
PLUG: Frame = (_rpy(0.2, 0.9, -0.4), (0.0, 0.03, -0.02))  # the plug's frame on the cable
HOME: Frame = (_rpy(math.pi, 0.1, 0.5), (0.3, -0.1, 1.5))  # the gripper's TCP in world


def _spawn(trial: dict[str, Any], rail: str = "nic_rail_0") -> WorldReady:
    """The poses the oracle would read in the robot's base frame once `trial` spawned, its
    target entity on `rail`."""
    board = trial["scene"]["task_board"]["pose"]
    entity = trial["scene"]["task_board"][rail]["entity_pose"]
    in_world: Frame = (
        _rpy(board["roll"], board["pitch"], board["yaw"]),
        (board["x"], board["y"], board["z"]),
    )
    along: Frame = (_rpy(0.0, 0.0, 0.0), (entity["translation"], 0.0, 0.0))
    turned: Frame = (_rpy(entity["roll"], entity["pitch"], entity["yaw"]), (0.0, 0.0, 0.0))
    port = _then(_then(_then(_then(in_world, RAIL), along), turned), PORT)
    (only,) = trial["scene"]["cables"].values()
    cable = only["pose"]
    offset = cable["gripper_offset"]
    home = HOME[1]
    at: Frame = (
        _rpy(cable["roll"], cable["pitch"], cable["yaw"]),
        (home[0] + offset["x"], home[1] + offset["y"], home[2] + offset["z"]),
    )
    to_base = _inv(ROBOT)
    return WorldReady(
        sim_origin_s=0.0,
        port=_record(_then(to_base, port)),
        plug=_record(_then(to_base, _then(at, PLUG))),
        gripper=_record(_then(to_base, HOME)),
    )


def _edit(*changes: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    trial = copy.deepcopy(BASE)
    for change in changes:
        change(trial)
    return trial


def _entity(key: str, value: float) -> Callable[[dict[str, Any]], None]:
    def change(trial: dict[str, Any]) -> None:
        trial["scene"]["task_board"]["nic_rail_0"]["entity_pose"][key] = value

    return change


def _cable(key: str, by: float) -> Callable[[dict[str, Any]], None]:
    def change(trial: dict[str, Any]) -> None:
        pose = trial["scene"]["cables"]["cable_0"]["pose"]
        if key in ("x", "y", "z"):
            pose["gripper_offset"][key] += by
        else:
            pose[key] += by

    return change


def _drawn_board(trial: dict[str, Any]) -> None:
    """A board pose drawn alongside the change, as ADR 030's draws do."""
    pose = trial["scene"]["task_board"]["pose"]
    pose["x"] += 0.012
    pose["y"] -= 0.031
    pose["yaw"] += 0.1


def _tilted_board(trial: dict[str, Any]) -> None:
    """A board pose with roll and pitch too, which the module's own conversion must turn."""
    _drawn_board(trial)
    pose = trial["scene"]["task_board"]["pose"]
    pose["roll"] += 0.04
    pose["pitch"] -= 0.03


SCENES = {
    "tilt": (_edit(_entity("roll", math.radians(2.0)), _drawn_board), TILT, math.radians(2.0)),
    "tilt on a tilted board": (
        _edit(_entity("pitch", math.radians(2.0)), _tilted_board),
        TILT,
        math.radians(2.0),
    ),
    "shift on a tilted board": (_edit(_entity("translation", 0.03), _tilted_board), SHIFT, 0.006),
    "pitch tilt": (_edit(_entity("pitch", math.radians(4.0))), TILT, math.radians(4.0)),
    "yaw": (_edit(_entity("yaw", math.radians(-10.0)), _drawn_board), YAW, math.radians(10.0)),
    "shift": (_edit(_entity("translation", -0.048), _drawn_board), SHIFT, 0.084),
    "grasp shift": (_edit(_cable("x", 0.005), _drawn_board), GRASP_SHIFT, 0.005),
    "grasp turn": (_edit(_cable("roll", 0.12), _drawn_board), GRASP_TURN, 0.12),
}


@pytest.mark.parametrize("name", sorted(SCENES))
def test_a_change_spawned_as_requested_reads_back_as_requested(name: str) -> None:
    scene, kind, size = SCENES[name]

    change = requested_change(scene, BASE)

    assert change is not None and change.kind == kind
    assert change.size == pytest.approx(size, abs=1e-12)
    reading = read_back(change, scene, _spawn(scene), BASE, _spawn(BASE))
    assert reading is not None and reading.applied
    assert reading.measured == pytest.approx(size, abs=1e-9)
    if kind == GRASP_SHIFT:
        assert reading.turn == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("name", sorted(SCENES))
def test_a_change_the_spawn_dropped_is_not_applied(name: str) -> None:
    scene, _, _ = SCENES[name]
    change = requested_change(scene, BASE)
    assert change is not None
    dropped = copy.deepcopy(BASE)
    dropped["scene"]["task_board"]["pose"] = scene["scene"]["task_board"]["pose"]  # board only

    reading = read_back(change, scene, _spawn(dropped), BASE, _spawn(BASE))

    assert reading is not None and not reading.applied
    assert reading.measured == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize(
    ("spawned_deg", "applied_"), [(2.24, True), (1.76, True), (2.26, False), (1.74, False)]
)
def test_a_tilt_is_applied_within_a_quarter_degree(spawned_deg: float, applied_: bool) -> None:
    scene = _edit(_entity("roll", math.radians(2.0)))
    spawned = _edit(_entity("roll", math.radians(spawned_deg)))
    change = requested_change(scene, BASE)
    assert change is not None

    reading = read_back(change, scene, _spawn(spawned), BASE, _spawn(BASE))

    assert reading is not None and reading.applied is applied_


@pytest.mark.parametrize(("off_m", "applied_"), [(0.0009, True), (0.0011, False)])
def test_a_shift_is_applied_within_a_millimetre(off_m: float, applied_: bool) -> None:
    scene = _edit(_entity("translation", -0.048))
    spawned = _edit(_entity("translation", -0.048 + off_m))
    change = requested_change(scene, BASE)
    assert change is not None

    reading = read_back(change, scene, _spawn(spawned), BASE, _spawn(BASE))

    assert reading is not None and reading.applied is applied_


@pytest.mark.parametrize(("deg", "applied_"), [(10.45, True), (10.55, False)])
def test_a_yaw_is_applied_within_half_a_degree(deg: float, applied_: bool) -> None:
    scene = _edit(_entity("yaw", math.radians(10.0)))
    spawned = _edit(_entity("yaw", math.radians(deg)))
    change = requested_change(scene, BASE)
    assert change is not None

    reading = read_back(change, scene, _spawn(spawned), BASE, _spawn(BASE))

    assert reading is not None and reading.applied is applied_


def test_a_grasp_shift_whose_plug_also_turned_is_not_applied() -> None:
    scene = _edit(_cable("x", 0.005))
    spawned = _edit(_cable("x", 0.005), _cable("yaw", 0.03))
    change = requested_change(scene, BASE)
    assert change is not None

    reading = read_back(change, scene, _spawn(spawned), BASE, _spawn(BASE))

    assert reading is not None and not reading.applied
    assert reading.turn == pytest.approx(0.03, abs=1e-9)


def test_the_board_pose_is_read_in_the_boards_frame_not_the_robots() -> None:
    """Read in the base frame, the drawn board's 0.1 rad would show as a 5.7° turn."""
    scene, _, _ = SCENES["tilt"]
    change = requested_change(scene, BASE)
    assert change is not None
    unboarded = copy.deepcopy(scene)
    unboarded["scene"]["task_board"]["pose"] = BASE["scene"]["task_board"]["pose"]

    reading = read_back(change, unboarded, _spawn(scene), BASE, _spawn(BASE))

    assert reading is not None and not reading.applied  # the scene's own board pose is needed


def _set(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def change(trial: dict[str, Any]) -> None:
        node = trial
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return change


def _drop(path: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def change(trial: dict[str, Any]) -> None:
        node = trial
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return change


ENTITY = ("scene", "task_board", "nic_rail_0", "entity_pose")
CABLE = ("scene", "cables", "cable_0", "pose")


@pytest.mark.parametrize(
    "changes",
    [
        (),
        (_drawn_board,),
        (_entity("roll", 0.03), _entity("yaw", 0.1)),
        (_entity("roll", 0.03), _cable("roll", 0.1)),
        (_cable("roll", 0.1), _cable("yaw", 0.1)),
        (_entity("roll", 0.03), _set(("task", "port_name"), "sfp_port_1")),
        (
            _entity("roll", 0.03),
            _set(("scene", "task_board", "sc_rail_0", "entity_present"), False),
        ),
        (_entity("roll", 0.03), _set(("scene", "cables", "cable_0", "cable_type"), "reversed")),
        (_set((*ENTITY, "roll"), "0.03"),),
        (_set((*ENTITY, "roll"), True),),
        (_set((*ENTITY, "roll"), math.nan),),
        (_set((*ENTITY, "roll"), 10**400),),
        (_drop((*ENTITY, "roll")),),
        (_set((*ENTITY, "spin"), 0.1),),
        (_set((*CABLE, "gripper_offset", "x"), None),),
    ],
    ids=[
        "no change",
        "the board pose only",
        "two entity values",
        "entity and cable",
        "two cable angles",
        "another port",
        "another rail",
        "another cable type",
        "a string",
        "a bool",
        "NaN",
        "an int past a float's range",
        "a key missing",
        "a key added",
        "a None offset",
    ],
)
def test_a_request_that_does_not_change_exactly_one_value_is_not_checked(
    changes: tuple[Callable[[dict[str, Any]], None], ...],
) -> None:
    assert requested_change(_edit(*changes), BASE) is None


@pytest.mark.parametrize("missing", ["port", "plug", "gripper"])
def test_a_pose_the_oracle_did_not_read_gives_no_reading(missing: str) -> None:
    for name in ("tilt", "grasp turn"):
        scene, kind, _ = SCENES[name]
        change = Change(kind, 0.1)
        ready = _spawn(scene).model_copy(update={missing: None})
        needed = missing == "port" if kind == TILT else missing != "port"

        for a, b in ((ready, _spawn(BASE)), (_spawn(scene), ready)):
            reading = read_back(change, scene, a, BASE, b)
            assert (reading is None) is needed


def test_a_zero_quaternion_gives_no_reading() -> None:
    scene, _, _ = SCENES["tilt"]
    change = requested_change(scene, BASE)
    assert change is not None
    ready = _spawn(scene).model_copy(
        update={"port": PoseRecord(position=(0.0, 0.0, 0.0), orientation=(0.0, 0.0, 0.0, 0.0))}
    )

    assert read_back(change, scene, ready, BASE, _spawn(BASE)) is None


@pytest.mark.parametrize("yaw", ["x", 10**400], ids=["a string", "an int past a float's range"])
def test_a_board_pose_that_is_not_numbers_gives_no_reading(yaw: object) -> None:
    scene = _edit(_entity("roll", 0.03), _set(("scene", "task_board", "pose", "yaw"), yaw))
    change = Change(TILT, 0.03)

    assert read_back(change, scene, _spawn(BASE), BASE, _spawn(BASE)) is None


def test_an_sc_port_is_read_on_its_own_rail() -> None:
    def tilt(trial: dict[str, Any]) -> None:
        trial["scene"]["task_board"]["sc_rail_1"]["entity_pose"]["roll"] = math.radians(2.0)

    scene = copy.deepcopy(SC_BASE)
    tilt(scene)
    _drawn_board(scene)

    change = requested_change(scene, SC_BASE)

    assert change is not None and change.kind == TILT
    assert change.size == pytest.approx(math.radians(2.0), abs=1e-12)
    spawned, base = _spawn(scene, rail="sc_rail_1"), _spawn(SC_BASE, rail="sc_rail_1")
    reading = read_back(change, scene, spawned, SC_BASE, base)
    assert reading is not None and reading.applied
    assert reading.measured == pytest.approx(math.radians(2.0), abs=1e-9)


# --- the nominal spread ----------------------------------------------------------------------


def test_the_nominal_spread_is_the_farthest_reading_of_each_kind() -> None:
    turned = _spawn(_edit(_entity("roll", math.radians(0.1))))  # the request unchanged: BASE
    shifted = _spawn(_edit(_cable("x", 0.0004)))
    missing = _spawn(BASE).model_copy(update={"plug": None})

    s = spread(BASE, _spawn(BASE), [(BASE, turned), (BASE, shifted), (BASE, missing)])

    assert s is not None and s.trials == 2  # the trial without a plug is skipped
    assert s.port_turn == pytest.approx(math.radians(0.1), abs=1e-9)
    assert s.plug_shift == pytest.approx(0.0004, abs=1e-9)
    assert s.plug_turn == pytest.approx(0.0, abs=1e-9)
    assert not s.wider_than_a_tolerance


@pytest.mark.parametrize(
    "drift",
    [
        _entity("roll", math.radians(0.3)),  # past the tilt's 0.25°
        _entity("translation", 0.036 + 0.0012),  # past the shift's 1 mm (the sample's 0.036)
        _cable("y", 0.0012),  # past the grasp shift's 1 mm
        _cable("yaw", 0.025),  # past the grasp turn's 0.02 rad
    ],
    ids=["port turn", "port shift", "plug shift", "plug turn"],
)
def test_a_spread_past_a_tolerance_is_wider_than_it(
    drift: Callable[[dict[str, Any]], None],
) -> None:
    s = spread(BASE, _spawn(BASE), [(BASE, _spawn(_edit(drift)))])

    assert s is not None and s.wider_than_a_tolerance


def test_a_reference_that_gives_no_reading_has_no_spread() -> None:
    ready = _spawn(BASE).model_copy(update={"port": None})

    assert spread(BASE, ready, [(BASE, _spawn(BASE))]) is None


@pytest.mark.parametrize(
    ("family", "checked"),
    [
        ("tilt_sfp", True),
        ("tilt_sc", True),
        ("yaw_edge_sfp", True),
        ("trans_edge_sc", True),
        ("beyond_sfp", True),
        ("inrange_sfp", False),
        ("empty_rail_sfp", False),
        ("reversed_sfp", False),
        ("nominal_sfp", False),
        ("tilted", False),
    ],
)
def test_only_the_families_that_change_one_value_are_checked(family: str, checked: bool) -> None:
    assert checked_family(family) is checked


def test_the_robot_base_is_the_launch_default() -> None:
    assert applied.ROBOT_BASE_IN_WORLD == (-0.2, 0.2, 1.14, 0.0, 0.0, -3.141)
