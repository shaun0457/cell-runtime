"""The Phase 2 scene manifests (ADR 026, ADR 030, #80), drawn with fixed seeds.

Three splits, one manifest each (ADR 027), written under `eval/sim/`:
- `pilot-v1`: ADR 026's 23 scenes. Its fixed-value scenes change one value of their base scene;
  its `inrange` scenes draw every parameter at once.
- `tune-v1` and `heldout-v1`: 4 scenes for each of ADR 030's ten families. A family with a fixed
  value keeps it, draws the choice ADR 030 gives it (a sign, one of its values, a roll in
  [2°, 4°], the rail left empty), and draws the board pose. `tune-v1` adds the two `nominal`
  scenes, which are the pilot's own (ADR 030: their digests are the only ones two splits share).

The base scenes are the sample config's `trial_1` (SFP) and `trial_3` (SC), read from the file
given (`tests/fixtures/sim/sample_config.yaml` is an unchanged copy).

The draws of scene i of a family come from `random.Random(f"{split}:{family}:{i}")`, and only
through its `random()` method: Python keeps that sequence the same for a given seed across
versions, and promises nothing for `randint`, `choice` or `shuffle`. A drawn value is rounded to
1 µm or 1 µrad and kept inside its range; a fixed value is set exactly.

`queue.json` fixes the order the units run in:
- `pilot-v1`: `order` is the manifest's order of its scenes, ADR 026's table (the manifest file
  is written with sorted keys, so this list is where that order is kept; ADR 026's B pass breaks
  its ties by it). `A` holds every unit but `reversed`'s: repeat 0 of each scene in that order,
  then repeat 1. The B units are chosen at the session from the A results (ADR 026) and run
  next; `last` (`reversed`, with A) runs after them.
- `tune-v1` and `heldout-v1`: `A` and `B` each hold every unit, shuffled with
  `random.Random(f"{split}:order")`, A's order drawn first (ADR 030). Engine runs take up to
  `max_trials_per_run` units of one setting and alternate A and B, starting with A; the units of
  families the checkpoint did not select are skipped.

Engine configs are not written here. The runner renders each one from the manifest's own scene
entries and records its SHA-256 in the ledger (ADR 028); every scene is rendered and validated
here, so none can stop an engine run at parse time. `SHA256SUMS` holds the SHA-256 of the
manifests, the queue and the engine template, in `sha256sum -c` form.

    python -m tools.sim.generate --sample tests/fixtures/sim/sample_config.yaml --out eval/sim

`--check` compares the generated files with the ones in `--out` instead of writing them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.sim.scenes import (
    CLAMP_M,
    EngineConfigError,
    Scene,
    build_manifest,
    engine_config,
    parse_template,
    read_bounded,
    scene_from_trial,
)

RngFor = Callable[[str], random.Random]
Trial = dict[str, Any]

PILOT, TUNE, HELDOUT = "pilot-v1", "tune-v1", "heldout-v1"
# ADR 030's ten families, in its order (the checkpoint breaks its ties in this order).
FAMILIES = (
    "inrange_sfp",
    "inrange_sc",
    "yaw_edge_sfp",
    "trans_edge_sfp",
    "trans_edge_sc",
    "beyond_sfp",
    "beyond_sc",
    "tilt_sfp",
    "tilt_sc",
    "empty_rail_sfp",
)
NOMINAL = ("nominal_sfp", "nominal_sc")
REVERSED = "reversed_sfp"
SCENES_PER_FAMILY = 4
PILOT_REPEATS = 2  # ADR 026: A twice per scene, `reversed` once
TUNE_REPEATS = 1  # ADR 030: once with A and once with B
NOMINAL_TUNE_REPEATS = 6  # ADR 030: the nominal scenes' clean repeats, per setting
HELDOUT_REPEATS = 3  # ADR 030: seeds 0-2
MAX_TRIALS_PER_RUN = 6  # ADR 028
DIGITS = 6  # 1 µm, 1 µrad

TEMPLATE = "engine_template.yaml"
QUEUE = "queue.json"
SUMS = "SHA256SUMS"

# ADR 026's ranges, as half-widths around the base scene. None is published for the board pose:
# those are ours, and span the sample's own trials.
BOARD_HALF = {"x": 0.02, "y": 0.05, "yaw": 0.15}
GRASP_OFFSET_HALF_M = 0.002
GRASP_ANGLE_HALF_RAD = 0.04
NIC_YAW_EDGE_RAD = math.radians(10.0)
NIC_RAILS = 5
SC_RAILS = 2
SFP_PORTS = ("sfp_port_0", "sfp_port_1")
BEYOND_NIC_YAW_RAD = math.radians(20.0)
BEYOND_OFFSET_X_M = 0.005
BEYOND_ROLL_RAD = 0.12
TILT_RAD = (math.radians(2.0), math.radians(4.0))

HYPOTHESES = {
    "nominal_sfp": (
        "The sample's trial_1 unchanged, the SFP control: expected to seat, as in all 6 SFP "
        "tasks of Phase 1's three clean runs."
    ),
    "nominal_sc": (
        "The sample's trial_3 unchanged, the SC control: expected to seat most times; in "
        "Phase 1's three clean runs the SC plug stuck on the entrance rim once."
    ),
    "inrange_sfp": (
        "Inside the challenge's own ranges. The skills read the oracle's poses every step, so "
        "the change is followed: expected compensated."
    ),
    "inrange_sc": (
        "Inside the challenge's own ranges. The skills read the oracle's poses every step, so "
        "the change is followed: expected compensated."
    ),
    "yaw_edge_sfp": (
        "The NIC card at an end of its ±10° yaw range. The plug is turned to the port's "
        "orientation: expected compensated."
    ),
    "trans_edge_sfp": (
        "The NIC card at the far end of the engine's rail clamp. The port is followed wherever "
        "it sits: expected compensated."
    ),
    "trans_edge_sc": (
        "The SC port at the far end of the engine's rail clamp. The port is followed wherever "
        "it sits: expected compensated."
    ),
    "beyond_sfp": (
        "A value past the challenge's range: NIC yaw 20°, a grasp 5 mm off in x, or grasp roll "
        "+0.12 rad. The oracle still gives every pose: expected compensated."
    ),
    "beyond_sc": (
        "A grasp 5 mm off in x, past the challenge's ~2 mm. The plug's pose comes from the "
        "oracle: expected compensated."
    ),
    "tilt_sfp": (
        "The NIC card rolled 2-4°. The descent runs along base z above the port origin, so the "
        "entrance, 45.8 mm up, sits 1.6-3.2 mm to the side: expected to stick at the entrance."
    ),
    "tilt_sc": (
        "The SC port rolled 2-4°. The entrance, 13.64 mm up, sits 0.48-0.95 mm to the side of "
        "the descent, where SC plugs stuck on the rim before: expected to stick, at times."
    ),
    "empty_rail_sfp": (
        "The target rail is empty, the card on the next rail: the target port has no frame. "
        "Expected adapter abort (ground_truth_unavailable); no recovery can help."
    ),
    "reversed_sfp": (
        "The cable's SC end in the gripper on an SFP task: expected to fail, with no recovery "
        "possible. Runs once, last: the free plug can leave the sim in a state others inherit."
    ),
}


@dataclass(frozen=True)
class Split:
    """One manifest's scenes, in their order, and each scene's number of repeats."""

    name: str
    scenes: tuple[Scene, ...]
    repeats: Mapping[str, int]

    def units(self) -> list[str]:
        return [s.task_id(k) for s in self.scenes for k in range(self.repeats[s.scene_id])]


# --- the generator ---------------------------------------------------------------------------


def splits(sample: Mapping[str, Any], rng_for: RngFor = random.Random) -> dict[str, Split]:
    """The three splits, from the sample config's base scenes."""
    sfp, sc = _bases(sample)
    nominal = (_scene("nominal_sfp", "nominal_sfp", sfp), _scene("nominal_sc", "nominal_sc", sc))
    pilot = (*nominal, *_pilot(sfp, sc, rng_for))
    tune = (*nominal, *_drawn("tune", sfp, sc, rng_for))
    heldout = _drawn("heldout", sfp, sc, rng_for)
    return {
        PILOT: Split(
            PILOT,
            pilot,
            {s.scene_id: 1 if s.family == REVERSED else PILOT_REPEATS for s in pilot},
        ),
        TUNE: Split(
            TUNE,
            tune,
            {
                s.scene_id: NOMINAL_TUNE_REPEATS if s.family in NOMINAL else TUNE_REPEATS
                for s in tune
            },
        ),
        HELDOUT: Split(HELDOUT, heldout, {s.scene_id: HELDOUT_REPEATS for s in heldout}),
    }


def build_queue(by_name: Mapping[str, Split], rng_for: RngFor = random.Random) -> dict[str, Any]:
    """The order the units run in (module docstring)."""
    pilot = by_name[PILOT]
    plain = [s for s in pilot.scenes if s.family != REVERSED]
    queue: dict[str, Any] = {
        PILOT: {
            "order": [s.scene_id for s in pilot.scenes],
            "A": [s.task_id(k) for k in range(PILOT_REPEATS) for s in plain],
            "last": [s.task_id(0) for s in pilot.scenes if s.family == REVERSED],
        }
    }
    for name, split in ((TUNE, "tune"), (HELDOUT, "heldout")):
        units = by_name[name].units()
        rng = rng_for(f"{split}:order")
        a = _shuffled(units, rng)
        queue[name] = {"A": a, "B": _shuffled(units, rng)}
    return {"version": 1, "max_trials_per_run": MAX_TRIALS_PER_RUN, "splits": queue}


def render(
    sample: Mapping[str, Any], template: Path, rng_for: RngFor = random.Random
) -> dict[str, bytes]:
    """Every file this module writes, by name. Each scene is rendered into a one-trial engine
    config and validated first (EngineConfigError if one fails)."""
    by_name = splits(sample, rng_for)
    template_text = read_bounded(template)  # read once: the sums hash the bytes checked here
    sections = parse_template(template_text)
    for split in by_name.values():
        for scene in split.scenes:
            engine_config([(scene, 0)], sections)
    files = {
        f"{split.name}.json": _json(build_manifest(split.name, split.scenes, repeats=split.repeats))
        for split in by_name.values()
    }
    files[QUEUE] = _json(build_queue(by_name, rng_for))
    hashed = {**files, TEMPLATE: template_text.encode("utf-8")}
    files[SUMS] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(hashed.items())
    ).encode("ascii")
    return files


# --- the scenes ------------------------------------------------------------------------------


def _bases(sample: Mapping[str, Any]) -> tuple[Trial, Trial]:
    trials = sample["trials"]
    sfp = scene_from_trial("base", "base", trials["trial_1"]).trial
    sc = scene_from_trial("base", "base", trials["trial_3"]).trial
    if (sfp["task"]["target_module_name"], sfp["task"]["port_name"]) != (
        "nic_card_mount_0",
        "sfp_port_0",
    ) or sc["task"]["target_module_name"] != "sc_port_1":
        raise EngineConfigError("sample: trial_1 and trial_3 are not the base scenes of ADR 026")
    return sfp, sc


def _pilot(sfp: Trial, sc: Trial, rng_for: RngFor) -> list[Scene]:
    """ADR 026's table, after the two nominal scenes."""
    scenes = [
        *(
            _scene(f"{family}_{i}", family, draw(rng_for(f"pilot:{family}:{i}"), base))
            for family, draw, base in (
                ("inrange_sfp", _inrange_sfp, sfp),
                ("inrange_sc", _inrange_sc, sc),
            )
            for i in range(SCENES_PER_FAMILY)
        ),
        _scene("yaw_edge_sfp_0", "yaw_edge_sfp", _edit(sfp, _nic_yaw, -NIC_YAW_EDGE_RAD)),
        _scene("yaw_edge_sfp_1", "yaw_edge_sfp", _edit(sfp, _nic_yaw, NIC_YAW_EDGE_RAD)),
        _scene("trans_edge_sfp_0", "trans_edge_sfp", _edit(sfp, _far_end, "nic")),
        _scene("trans_edge_sc_0", "trans_edge_sc", _edit(sc, _far_end, "sc")),
        *(
            _scene(f"beyond_sfp_{i}", "beyond_sfp", _edit(sfp, beyond))
            for i, beyond in enumerate(_BEYOND_SFP)
        ),
        _scene("beyond_sc_0", "beyond_sc", _edit(sc, _BEYOND_SC[0])),
        _scene("tilt_sfp_0", "tilt_sfp", _edit(sfp, _tilt, TILT_RAD[0])),
        _scene("tilt_sc_0", "tilt_sc", _edit(sc, _tilt, TILT_RAD[0])),
        _scene("tilt_sc_1", "tilt_sc", _edit(sc, _tilt, TILT_RAD[1])),
        _scene("empty_rail_sfp_0", "empty_rail_sfp", _edit(sfp, _empty_rail, 0)),
        _scene("reversed_sfp_0", REVERSED, _edit(sfp, _reversed)),
    ]
    return scenes


def _drawn(split: str, sfp: Trial, sc: Trial, rng_for: RngFor) -> list[Scene]:
    """4 scenes of each family, drawn per ADR 030."""
    return [
        _scene(f"{family}_{i}", family, _DRAW[family](rng_for(f"{split}:{family}:{i}"), sfp, sc))
        for family in FAMILIES
        for i in range(SCENES_PER_FAMILY)
    ]


def _scene(scene_id: str, family: str, trial: Trial) -> Scene:
    return Scene(scene_id=scene_id, family=family, hypothesis=HYPOTHESES[family], trial=trial)


# A family with a fixed value keeps it and draws the board pose; ADR 030's choices come first.
def _draw_yaw_edge(rng: random.Random, sfp: Trial, sc: Trial) -> Trial:
    sign = -1.0 if rng.random() < 0.5 else 1.0
    return _boarded(rng, _edit(sfp, _nic_yaw, sign * NIC_YAW_EDGE_RAD))


def _draw_beyond_sfp(rng: random.Random, sfp: Trial, sc: Trial) -> Trial:
    return _boarded(rng, _edit(sfp, _BEYOND_SFP[_index(rng, len(_BEYOND_SFP))]))


def _draw_beyond_sc(rng: random.Random, sfp: Trial, sc: Trial) -> Trial:
    return _boarded(rng, _edit(sc, _BEYOND_SC[_index(rng, len(_BEYOND_SC))]))


def _draw_tilt(base: str) -> Callable[[random.Random, Trial, Trial], Trial]:
    def draw(rng: random.Random, sfp: Trial, sc: Trial) -> Trial:
        return _boarded(rng, _edit(sfp if base == "sfp" else sc, _tilt, _uniform(rng, *TILT_RAD)))

    return draw


def _draw_empty_rail(rng: random.Random, sfp: Trial, sc: Trial) -> Trial:
    return _boarded(rng, _edit(sfp, _empty_rail, _index(rng, NIC_RAILS)))


_DRAW: dict[str, Callable[[random.Random, Trial, Trial], Trial]] = {
    "inrange_sfp": lambda rng, sfp, sc: _inrange_sfp(rng, sfp),
    "inrange_sc": lambda rng, sfp, sc: _inrange_sc(rng, sc),
    "yaw_edge_sfp": _draw_yaw_edge,
    "trans_edge_sfp": lambda rng, sfp, sc: _boarded(rng, _edit(sfp, _far_end, "nic")),
    "trans_edge_sc": lambda rng, sfp, sc: _boarded(rng, _edit(sc, _far_end, "sc")),
    "beyond_sfp": _draw_beyond_sfp,
    "beyond_sc": _draw_beyond_sc,
    "tilt_sfp": _draw_tilt("sfp"),
    "tilt_sc": _draw_tilt("sc"),
    "empty_rail_sfp": _draw_empty_rail,
}


def _inrange_sfp(rng: random.Random, base: Trial) -> Trial:
    trial = _boarded(rng, copy.deepcopy(base))
    _draw_grasp(rng, trial)
    _move(trial, "nic", _index(rng, NIC_RAILS))
    trial["task"]["port_name"] = SFP_PORTS[_index(rng, len(SFP_PORTS))]
    pose = _target_pose(trial)
    pose["translation"] = _uniform(rng, *CLAMP_M["nic"])
    pose["yaw"] = _uniform(rng, -NIC_YAW_EDGE_RAD, NIC_YAW_EDGE_RAD)
    return trial


def _inrange_sc(rng: random.Random, base: Trial) -> Trial:
    trial = _boarded(rng, copy.deepcopy(base))
    _draw_grasp(rng, trial)
    _move(trial, "sc", _index(rng, SC_RAILS))
    _target_pose(trial)["translation"] = _uniform(rng, *CLAMP_M["sc"])
    return trial


# --- edits of a trial ------------------------------------------------------------------------


def _edit(base: Trial, change: Callable[..., None], *args: Any) -> Trial:
    trial = copy.deepcopy(base)
    change(trial, *args)
    return trial


def _nic_yaw(trial: Trial, yaw: float) -> None:
    _target_pose(trial)["yaw"] = yaw


def _far_end(trial: Trial, kind: str) -> None:
    """The clamp end farther from where the base sits, on the base's own rail (ADR 026)."""
    pose = _target_pose(trial)
    lo, hi = CLAMP_M[kind]
    pose["translation"] = (
        lo if abs(pose["translation"] - lo) > abs(pose["translation"] - hi) else hi
    )


def _tilt(trial: Trial, roll: float) -> None:
    _target_pose(trial)["roll"] = roll


def _empty_rail(trial: Trial, empty: int) -> None:
    """The task's target rail left empty, the card on the next rail."""
    _move(trial, "nic", (empty + 1) % NIC_RAILS)
    trial["task"]["target_module_name"] = f"nic_card_mount_{empty}"


def _reversed(trial: Trial) -> None:
    _cable(trial)["cable_type"] = "sfp_sc_cable_reversed"


def _yaw_beyond(trial: Trial) -> None:
    _target_pose(trial)["yaw"] += BEYOND_NIC_YAW_RAD


def _offset_beyond(trial: Trial) -> None:
    offset = _cable(trial)["pose"]["gripper_offset"]
    offset["x"] = _round(offset["x"] + BEYOND_OFFSET_X_M)


def _roll_beyond(trial: Trial) -> None:
    pose = _cable(trial)["pose"]
    pose["roll"] = _round(pose["roll"] + BEYOND_ROLL_RAD)


_BEYOND_SFP = (_yaw_beyond, _offset_beyond, _roll_beyond)
_BEYOND_SC = (_offset_beyond,)


def _boarded(rng: random.Random, trial: Trial) -> Trial:
    pose = _board(trial)["pose"]
    for key in ("x", "y", "yaw"):
        pose[key] = _around(rng, pose[key], BOARD_HALF[key])
    return trial


def _draw_grasp(rng: random.Random, trial: Trial) -> None:
    pose = _cable(trial)["pose"]
    for axis in ("x", "y", "z"):
        pose["gripper_offset"][axis] = _around(
            rng, pose["gripper_offset"][axis], GRASP_OFFSET_HALF_M
        )
    for angle in ("roll", "pitch", "yaw"):
        pose[angle] = _around(rng, pose[angle], GRASP_ANGLE_HALF_RAD)


def _move(trial: Trial, kind: str, rail: int) -> None:
    """The task's target entity, pose kept, onto `rail`, and the task pointed at it. aic_engine
    names the module by the rail (`nic_card_mount_<i>`, `sc_port_<i>`, aic_engine.cpp:1817,
    :1847); `entity_name` follows the sample's own naming."""
    board = _board(trial)
    entity = board[_target_key(trial)]
    board[_target_key(trial)] = {"entity_present": False}
    prefix, name, module = {
        "nic": ("nic_rail", "nic_card", "nic_card_mount"),
        "sc": ("sc_rail", "sc_mount", "sc_port"),
    }[kind]
    board[f"{prefix}_{rail}"] = {
        "entity_present": True,
        "entity_name": f"{name}_{rail}",
        "entity_pose": entity["entity_pose"],
    }
    trial["task"]["target_module_name"] = f"{module}_{rail}"


def _board(trial: Trial) -> dict[str, Any]:
    return trial["scene"]["task_board"]


def _cable(trial: Trial) -> dict[str, Any]:
    (cable,) = trial["scene"]["cables"].values()
    return cable


_RAIL_OF_MODULE = {"nic_card_mount": "nic_rail", "sc_port": "sc_rail"}


def _target_key(trial: Trial) -> str:
    """The rail key of the task's target module: `nic_card_mount_2` sits on `nic_rail_2`."""
    module, _, index = trial["task"]["target_module_name"].rpartition("_")
    return f"{_RAIL_OF_MODULE[module]}_{index}"


def _target_pose(trial: Trial) -> dict[str, Any]:
    return _board(trial)[_target_key(trial)]["entity_pose"]


# --- draws, through `random()` only ----------------------------------------------------------


def _round(value: float) -> float:
    return round(value, DIGITS)


def _uniform(rng: random.Random, lo: float, hi: float) -> float:
    return min(max(_round(lo + (hi - lo) * rng.random()), lo), hi)


def _around(rng: random.Random, base: float, half: float) -> float:
    return _uniform(rng, _round(base - half), _round(base + half))


def _index(rng: random.Random, n: int) -> int:
    return min(int(rng.random() * n), n - 1)


def _shuffled(items: Sequence[str], rng: random.Random) -> list[str]:
    """Fisher-Yates through `random()`."""
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = _index(rng, i + 1)
        out[i], out[j] = out[j], out[i]
    return out


def _json(data: Mapping[str, Any]) -> bytes:
    text = json.dumps(data, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False)
    return (text + "\n").encode("ascii")


# --- the command -----------------------------------------------------------------------------


def _holds(path: Path, data: bytes) -> bool:
    """Whether `path` is a regular file holding exactly `data`; reads at most one byte past it.
    A file that exists but cannot be read raises OSError: unreadable is not the same as stale."""
    if not path.is_file():
        return False
    with path.open("rb") as source:
        return source.read(len(data) + 1) == data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.sim.generate")
    parser.add_argument("--sample", type=Path, required=True, help="aic_engine's sample config")
    parser.add_argument("--out", type=Path, required=True, help="the directory of the template")
    parser.add_argument("--check", action="store_true", help="compare instead of writing")
    args = parser.parse_args(argv)
    try:
        files = render(yaml.safe_load(read_bounded(args.sample)), args.out / TEMPLATE)
    except EngineConfigError as ex:  # our own message
        print(f"generate: {ex}", file=sys.stderr)
        return 2
    except Exception as ex:  # a file we could not read or parse: its text is not ours to print
        print(f"generate: the inputs could not be read ({type(ex).__name__})", file=sys.stderr)
        return 2
    if args.check:
        stale = []
        for name, data in files.items():
            try:
                if not _holds(args.out / name, data):
                    stale.append(name)
            except OSError as ex:  # a file we could not read: its error text is not ours to print
                print(f"generate: {name} could not be read ({type(ex).__name__})", file=sys.stderr)
                return 2
        for name in stale:
            print(f"generate: {name} differs from the generated file", file=sys.stderr)
        return 1 if stale else 0
    for name, data in files.items():
        (args.out / name).write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
