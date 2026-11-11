"""The Phase 2 scene manifests (`tools/sim/generate.py`, ADR 026, ADR 030, #80).

The committed files under `eval/sim/` are the pre-registration (D4): they must be exactly what
the generator writes, and their digests are pinned here, so a changed scene, seed or generator
shows up as a failing test and a changed golden digest in review.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

import tools.sim.generate as generate
from adapters.aic.manifest import load_manifest, scene_digest
from tools.sim.generate import (
    FAMILIES,
    HELDOUT,
    PILOT,
    QUEUE,
    SUMS,
    TEMPLATE,
    TUNE,
    Split,
    main,
    render,
    splits,
)
from tools.sim.scenes import CLAMP_M, EngineConfigError, engine_config, load_template, scenes_of

REPO = Path(__file__).parent.parent
EVAL_SIM = REPO / "eval" / "sim"
SAMPLE = Path(__file__).parent / "fixtures" / "sim" / "sample_config.yaml"

GOLDEN = {
    "pilot-v1.json": "172241ca05c2fdc5514a4aea5f2c8ad6070b7110507c3adaf12b77df0127ee57",
    "tune-v1.json": "ba8174d1e948968d4eb3bfbc3428d252210000bb94fbc11a0fd32b59fa5a0739",
    "heldout-v1.json": "590fd652fe619328f1bedd7286cbd6aff6c1f31269ea7d00717f189142516c13",
    "queue.json": "c27a560d0421996e8965f20b10a4676bbb95d3601d6f75edbc5e15d7c5396359",
}
# ADR 026's ranges, written out here rather than imported, so a wrong constant in the generator
# fails these tests: half-widths around the base scene.
BOARD_HALF = {"x": 0.02, "y": 0.05, "yaw": 0.15}
GRASP_OFFSET_HALF_M = 0.002
GRASP_ANGLE_HALF_RAD = 0.04
# ADR 026's table, in its order.
PILOT_ORDER = [
    "nominal_sfp",
    "nominal_sc",
    *(f"inrange_sfp_{i}" for i in range(4)),
    *(f"inrange_sc_{i}" for i in range(4)),
    "yaw_edge_sfp_0",
    "yaw_edge_sfp_1",
    "trans_edge_sfp_0",
    "trans_edge_sc_0",
    "beyond_sfp_0",
    "beyond_sfp_1",
    "beyond_sfp_2",
    "beyond_sc_0",
    "tilt_sfp_0",
    "tilt_sc_0",
    "tilt_sc_1",
    "empty_rail_sfp_0",
    "reversed_sfp_0",
]

BOARD = "scene.task_board.pose"
NIC = "scene.task_board.nic_rail_{}"
SC = "scene.task_board.sc_rail_{}"
SFP_CABLE = "scene.cables.cable_0"
SC_CABLE = "scene.cables.cable_1"


def _sample() -> dict[str, Any]:
    return yaml.safe_load(SAMPLE.read_text("utf-8"))


def _base(port: str) -> dict[str, Any]:
    trial = _sample()["trials"]["trial_1" if port == "sfp" else "trial_3"]
    return {"scene": trial["scene"], "task": trial["tasks"]["task_1"]}


@pytest.fixture(scope="module")
def files() -> dict[str, bytes]:
    return render(_sample(), EVAL_SIM / TEMPLATE)


@pytest.fixture(scope="module")
def by_name() -> dict[str, Split]:
    return splits(_sample())


def _manifest(name: str) -> dict[str, Any]:
    return json.loads((EVAL_SIM / f"{name}.json").read_text("utf-8"))


def _leaves(node: Any, at: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _leaves(value, f"{at}.{key}" if at else str(key))
    else:
        yield at, node


def _changes(base: Mapping[str, Any], trial: Mapping[str, Any]) -> dict[str, Any]:
    """Every leaf of `trial` that differs from `base`, or that only one of them has."""
    a, b = dict(_leaves(base)), dict(_leaves(trial))
    return {
        k: b.get(k, "<absent>")
        for k in a.keys() | b.keys()
        if a.get(k, "<absent>") != b.get(k, "<absent>")
    }


def _scenes(name: str, family: str) -> list[tuple[str, dict[str, Any]]]:
    scenes = _manifest(name)["scenes"]
    return [(sid, e["trial"]) for sid, e in sorted(scenes.items()) if e["family"] == family]


def _port(family: str) -> str:
    return "sc" if family.endswith("_sc") else "sfp"


def _without_board(changes: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in changes.items() if not k.startswith(f"{BOARD}.")}


# --- the committed files ---------------------------------------------------------------------


def test_the_committed_files_are_the_generated_ones(files: dict[str, bytes]) -> None:
    assert set(files) == {*GOLDEN, SUMS}
    for name, data in files.items():
        assert (EVAL_SIM / name).read_bytes() == data, name


def test_the_manifests_and_the_queue_have_their_golden_digests() -> None:
    for name, digest in GOLDEN.items():
        assert hashlib.sha256((EVAL_SIM / name).read_bytes()).hexdigest() == digest, name


def test_the_sums_file_covers_the_manifests_the_queue_and_the_template() -> None:
    lines = (EVAL_SIM / SUMS).read_text("ascii").splitlines()
    listed = dict(reversed(line.split("  ")) for line in lines)

    assert set(listed) == {*GOLDEN, TEMPLATE}
    for name, digest in listed.items():
        assert hashlib.sha256((EVAL_SIM / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize("name", [PILOT, TUNE, HELDOUT])
def test_each_manifest_is_one_the_policy_loads(name: str) -> None:
    manifest = load_manifest(EVAL_SIM / f"{name}.json")

    assert manifest.name == name
    for entry in manifest.scenes.values():
        assert entry.expected_class == "unknown"
        assert 0 < len(entry.hypothesis) <= 300


# --- the splits ------------------------------------------------------------------------------


def test_the_pilot_is_adr_026s_table(by_name: dict[str, Split]) -> None:
    pilot = by_name[PILOT]

    assert Counter(s.family for s in pilot.scenes) == {
        "nominal_sfp": 1,
        "nominal_sc": 1,
        "inrange_sfp": 4,
        "inrange_sc": 4,
        "yaw_edge_sfp": 2,
        "trans_edge_sfp": 1,
        "trans_edge_sc": 1,
        "beyond_sfp": 3,
        "beyond_sc": 1,
        "tilt_sfp": 1,
        "tilt_sc": 2,
        "empty_rail_sfp": 1,
        "reversed_sfp": 1,
    }
    assert len(pilot.units()) == 45  # A twice on 22 scenes, `reversed` once
    assert {s.scene_id for s in pilot.scenes if pilot.repeats[s.scene_id] == 1} == {
        "reversed_sfp_0"
    }


def test_tuning_and_the_held_out_draw_4_scenes_of_each_family(by_name: dict[str, Split]) -> None:
    tune, heldout = by_name[TUNE], by_name[HELDOUT]

    assert Counter(s.family for s in heldout.scenes) == {f: 4 for f in FAMILIES}
    assert Counter(s.family for s in tune.scenes) == {
        **{f: 4 for f in FAMILIES},
        "nominal_sfp": 1,
        "nominal_sc": 1,
    }
    assert len(tune.units()) == 40 + 2 * 6  # ADR 030: 6 clean repeats per nominal scene
    assert len(heldout.units()) == 40 * 3  # seeds 0-2
    assert {k for s in heldout.scenes for k in range(heldout.repeats[s.scene_id])} == {0, 1, 2}


def test_no_scene_digest_is_in_two_splits_but_the_nominal_ones(by_name: dict[str, Split]) -> None:
    digests = {name: [s.digest for s in split.scenes] for name, split in by_name.items()}
    nominal = {s.digest for s in by_name[PILOT].scenes if s.family.startswith("nominal")}

    for name, listed in digests.items():
        assert len(set(listed)) == len(listed), name  # no scene twice in a split
    assert set(digests[PILOT]) & set(digests[TUNE]) == nominal
    assert not set(digests[PILOT]) & set(digests[HELDOUT])
    assert not set(digests[TUNE]) & set(digests[HELDOUT])


def test_the_nominal_scenes_are_the_samples_trials() -> None:
    scenes = _manifest(PILOT)["scenes"]

    assert scenes["nominal_sfp"]["digest"] == scene_digest(_base("sfp"))
    assert scenes["nominal_sc"]["digest"] == scene_digest(_base("sc"))


# --- the values ------------------------------------------------------------------------------

R10, R20, R2, R4 = (math.radians(d) for d in (10.0, 20.0, 2.0, 4.0))


@pytest.mark.parametrize(
    ("scene_id", "port", "expected"),
    [
        ("yaw_edge_sfp_0", "sfp", {f"{NIC.format(0)}.entity_pose.yaw": -R10}),
        ("yaw_edge_sfp_1", "sfp", {f"{NIC.format(0)}.entity_pose.yaw": R10}),
        ("trans_edge_sfp_0", "sfp", {f"{NIC.format(0)}.entity_pose.translation": -0.048}),
        ("trans_edge_sc_0", "sc", {f"{SC.format(1)}.entity_pose.translation": 0.055}),
        ("beyond_sfp_0", "sfp", {f"{NIC.format(0)}.entity_pose.yaw": R20}),
        ("beyond_sfp_1", "sfp", {f"{SFP_CABLE}.pose.gripper_offset.x": 0.005}),
        ("beyond_sfp_2", "sfp", {f"{SFP_CABLE}.pose.roll": 0.5632}),  # 0.4432 + 0.12
        ("beyond_sc_0", "sc", {f"{SC_CABLE}.pose.gripper_offset.x": 0.005}),
        ("tilt_sfp_0", "sfp", {f"{NIC.format(0)}.entity_pose.roll": R2}),
        ("tilt_sc_0", "sc", {f"{SC.format(1)}.entity_pose.roll": R2}),
        ("tilt_sc_1", "sc", {f"{SC.format(1)}.entity_pose.roll": R4}),
        ("reversed_sfp_0", "sfp", {f"{SFP_CABLE}.cable_type": "sfp_sc_cable_reversed"}),
    ],
)
def test_a_fixed_value_pilot_scene_changes_exactly_its_value(
    scene_id: str, port: str, expected: dict[str, Any]
) -> None:
    trial = _manifest(PILOT)["scenes"][scene_id]["trial"]

    assert _changes(_base(port), trial) == expected  # the value, exactly, and nothing else


def test_the_pilots_empty_rail_scene_moves_the_card_off_its_target() -> None:
    trial = _manifest(PILOT)["scenes"]["empty_rail_sfp_0"]["trial"]
    board = trial["scene"]["task_board"]
    base = _base("sfp")

    assert trial["task"] == base["task"]  # still nic_card_mount_0 / sfp_port_0
    assert board["nic_rail_0"] == {"entity_present": False}
    assert board["nic_rail_1"]["entity_present"] is True
    assert (
        board["nic_rail_1"]["entity_pose"]
        == base["scene"]["task_board"]["nic_rail_0"]["entity_pose"]
    )
    assert {k.split(".")[2] for k in _changes(base, trial)} == {"nic_rail_0", "nic_rail_1"}


def _check_board(base: Mapping[str, Any], trial: Mapping[str, Any]) -> None:
    pose, around = trial["scene"]["task_board"]["pose"], base["scene"]["task_board"]["pose"]
    for key, half in BOARD_HALF.items():
        assert abs(pose[key] - around[key]) <= half + 1e-6, key
    for key in ("z", "roll", "pitch"):
        assert pose[key] == around[key], key


def _present(board: Mapping[str, Any], kind: str, count: int) -> list[int]:
    return [i for i in range(count) if board[f"{kind}_rail_{i}"]["entity_present"]]


@pytest.mark.parametrize("name", [PILOT, TUNE, HELDOUT])
@pytest.mark.parametrize("family", ["inrange_sfp", "inrange_sc"])
def test_every_inrange_draw_lies_in_its_range(name: str, family: str) -> None:
    port = _port(family)
    base = _base(port)
    cable = SFP_CABLE if port == "sfp" else SC_CABLE
    scenes = _scenes(name, family)
    assert len(scenes) == 4
    rails = "scene.task_board.nic_rail_" if port == "sfp" else "scene.task_board.sc_rail_"
    drawn = {
        *(f"{BOARD}.{k}" for k in BOARD_HALF),
        *(f"{cable}.pose.gripper_offset.{a}" for a in "xyz"),
        *(f"{cable}.pose.{a}" for a in ("roll", "pitch", "yaw")),
        "task.target_module_name",
        *(["task.port_name"] if port == "sfp" else []),
    }
    for scene_id, trial in scenes:
        _check_board(base, trial)
        changed = set(_changes(base, trial))
        assert {k for k in changed if not k.startswith(rails)} <= drawn, scene_id  # nothing else
        flat, flat_base = dict(_leaves(trial)), dict(_leaves(base))
        for axis in ("x", "y", "z"):
            key = f"{cable}.pose.gripper_offset.{axis}"
            assert abs(flat[key] - flat_base[key]) <= GRASP_OFFSET_HALF_M + 1e-6, (scene_id, key)
        for angle in ("roll", "pitch", "yaw"):
            key = f"{cable}.pose.{angle}"
            assert abs(flat[key] - flat_base[key]) <= GRASP_ANGLE_HALF_RAD + 1e-6, (scene_id, key)
        board, task = trial["scene"]["task_board"], trial["task"]
        if port == "sfp":
            (rail,) = _present(board, "nic", 5)
            assert task["target_module_name"] == f"nic_card_mount_{rail}"
            assert task["port_name"] in ("sfp_port_0", "sfp_port_1")
            pose = board[f"nic_rail_{rail}"]["entity_pose"]
            lo, hi = CLAMP_M["nic"]
            assert lo <= pose["translation"] <= hi
            assert abs(pose["yaw"]) <= R10
            assert pose["roll"] == pose["pitch"] == 0.0
        else:
            (rail,) = _present(board, "sc", 2)
            assert task["target_module_name"] == f"sc_port_{rail}"
            pose = board[f"sc_rail_{rail}"]["entity_pose"]
            lo, hi = CLAMP_M["sc"]
            assert lo <= pose["translation"] <= hi
            assert (pose["roll"], pose["pitch"], pose["yaw"]) == (0.0, 0.0, 0.0)  # the base's


def test_the_inrange_draws_spread_over_the_rails() -> None:
    rails = {
        _present(trial["scene"]["task_board"], "nic", 5)[0]
        for name in (PILOT, TUNE, HELDOUT)
        for _, trial in _scenes(name, "inrange_sfp")
    }

    assert len(rails) > 1


def _fixed(family: str, changes: Mapping[str, Any]) -> bool:
    """Whether the changes off the board are the family's fixed value (ADR 030)."""
    nic, sc = f"{NIC.format(0)}.entity_pose", f"{SC.format(1)}.entity_pose"
    options = {
        "yaw_edge_sfp": [{f"{nic}.yaw": -R10}, {f"{nic}.yaw": R10}],
        "trans_edge_sfp": [{f"{nic}.translation": -0.048}],
        "trans_edge_sc": [{f"{sc}.translation": 0.055}],
        "beyond_sfp": [
            {f"{nic}.yaw": R20},
            {f"{SFP_CABLE}.pose.gripper_offset.x": 0.005},
            {f"{SFP_CABLE}.pose.roll": 0.5632},
        ],
        "beyond_sc": [{f"{SC_CABLE}.pose.gripper_offset.x": 0.005}],
    }[family]
    return dict(changes) in options


@pytest.mark.parametrize("name", [TUNE, HELDOUT])
@pytest.mark.parametrize(
    "family", ["yaw_edge_sfp", "trans_edge_sfp", "trans_edge_sc", "beyond_sfp", "beyond_sc"]
)
def test_a_fixed_value_family_keeps_its_value_and_draws_the_board(name: str, family: str) -> None:
    base = _base(_port(family))
    for scene_id, trial in _scenes(name, family):
        changes = _changes(base, trial)
        _check_board(base, trial)
        assert any(k.startswith(f"{BOARD}.") for k in changes), scene_id  # the board was drawn
        assert _fixed(family, _without_board(changes)), scene_id


@pytest.mark.parametrize("name", [TUNE, HELDOUT])
@pytest.mark.parametrize("family", ["tilt_sfp", "tilt_sc"])
def test_a_tilt_is_drawn_in_2_to_4_degrees(name: str, family: str) -> None:
    port = _port(family)
    base = _base(port)
    key = f"{NIC.format(0) if port == 'sfp' else SC.format(1)}.entity_pose.roll"
    for scene_id, trial in _scenes(name, family):
        _check_board(base, trial)
        changes = _without_board(_changes(base, trial))
        assert set(changes) == {key}, scene_id
        assert R2 <= changes[key] <= R4, scene_id


@pytest.mark.parametrize("name", [TUNE, HELDOUT])
def test_an_empty_rail_scene_leaves_its_target_rail_empty(name: str) -> None:
    base = _base("sfp")
    for scene_id, trial in _scenes(name, "empty_rail_sfp"):
        _check_board(base, trial)
        board = trial["scene"]["task_board"]
        target = int(trial["task"]["target_module_name"].rpartition("_")[2])
        assert _present(board, "nic", 5) == [(target + 1) % 5], scene_id
        assert (
            board[f"nic_rail_{(target + 1) % 5}"]["entity_pose"]
            == (base["scene"]["task_board"]["nic_rail_0"]["entity_pose"])
        )
        assert {
            k
            for k in _without_board(_changes(base, trial))
            if not k.startswith("scene.task_board.nic_rail_")
        } <= {"task.target_module_name"}, scene_id  # the rails, the board and the target only


# --- engine configs and the queue ------------------------------------------------------------


@pytest.mark.parametrize("name", [PILOT, TUNE, HELDOUT])
def test_every_engine_config_the_queue_plans_is_valid_and_carries_the_manifests_digests(
    name: str,
) -> None:
    manifest = _manifest(name)
    scenes = scenes_of(manifest)
    queue = json.loads((EVAL_SIM / QUEUE).read_text("utf-8"))
    size = queue["max_trials_per_run"]
    template = load_template(EVAL_SIM / TEMPLATE)
    lists = {k: v for k, v in queue["splits"][name].items() if k != "order"}  # order: scene ids
    for units in lists.values():
        for start in range(0, len(units), size):
            chunk = units[start : start + size]
            pairs = [
                (scenes[manifest["units"][u]["scene"]], manifest["units"][u]["seed"]) for u in chunk
            ]
            config = engine_config(pairs, template)  # validated inside
            for trial, unit in zip(config["trials"].values(), chunk, strict=True):
                ((task_id, task),) = trial["tasks"].items()
                assert task_id == unit
                digest = scene_digest({"scene": trial["scene"], "task": task})
                assert digest == manifest["scenes"][manifest["units"][unit]["scene"]]["digest"]


def test_the_pilot_queue_runs_a_twice_and_reversed_last(by_name: dict[str, Split]) -> None:
    queue = json.loads((EVAL_SIM / QUEUE).read_text("utf-8"))
    pilot = queue["splits"][PILOT]
    order = PILOT_ORDER[:-1]  # all but `reversed`

    assert queue["max_trials_per_run"] == 6
    assert set(pilot) == {"order", "A", "last"}  # B is chosen at the session (ADR 026)
    assert pilot["order"] == PILOT_ORDER == [s.scene_id for s in by_name[PILOT].scenes]
    assert pilot["A"] == [f"{s}_r0" for s in order] + [f"{s}_r1" for s in order]
    assert pilot["last"] == ["reversed_sfp_0_r0"]


@pytest.mark.parametrize("name", [TUNE, HELDOUT])
def test_tuning_and_the_held_out_queue_every_unit_once_per_setting_shuffled(
    name: str, by_name: dict[str, Split]
) -> None:
    split = json.loads((EVAL_SIM / QUEUE).read_text("utf-8"))["splits"][name]
    units = by_name[name].units()

    assert set(split) == {"A", "B"}
    for setting in ("A", "B"):
        assert sorted(split[setting]) == sorted(units)
        assert split[setting] != units  # shuffled, not the manifest's order
    assert split["A"] != split["B"]


# --- seeds -----------------------------------------------------------------------------------


class _OnlyRandom:
    """A generator with nothing but `random()`: any other draw (`uniform`, `choice`,
    `shuffle`, ...) is an AttributeError. Python keeps only `random()`'s sequence the same
    across versions for a seed."""

    def __init__(self, seed: str) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


def test_every_draw_goes_through_random_only(files: dict[str, bytes]) -> None:
    assert render(_sample(), EVAL_SIM / TEMPLATE, rng_for=_OnlyRandom) == files  # type: ignore[arg-type]


def test_each_scene_and_each_order_has_its_own_seed() -> None:
    seen: list[str] = []

    def rng_for(seed: str) -> random.Random:
        seen.append(seed)
        return random.Random(seed)

    render(_sample(), EVAL_SIM / TEMPLATE, rng_for=rng_for)

    expected = [
        *(f"pilot:{f}:{i}" for f in ("inrange_sfp", "inrange_sc") for i in range(4)),
        *(f"{split}:{f}:{i}" for split in ("tune", "heldout") for f in FAMILIES for i in range(4)),
        "tune:order",
        "heldout:order",
    ]
    assert sorted(seen) == sorted(expected)


def _swapping(pairs: Mapping[str, str]) -> Any:
    def rng_for(seed: str) -> random.Random:
        return random.Random(pairs.get(seed, seed))

    return rng_for


def test_each_split_draws_from_its_own_seeds() -> None:
    # The seed list above is sorted, so it cannot tell which scene drew which seed. Swapping
    # the two splits' seeds must swap their scenes: each scene is pinned to its own seed.
    pairs: dict[str, str] = {}
    for family in FAMILIES:
        for i in range(4):
            pairs[f"tune:{family}:{i}"] = f"heldout:{family}:{i}"
            pairs[f"heldout:{family}:{i}"] = f"tune:{family}:{i}"

    files = render(_sample(), EVAL_SIM / TEMPLATE, rng_for=_swapping(pairs))

    tune = json.loads(files[f"{TUNE}.json"])["scenes"]
    heldout = json.loads((EVAL_SIM / f"{HELDOUT}.json").read_bytes())["scenes"]
    assert {k: v for k, v in tune.items() if k in heldout} == heldout


def _plain(trial: Any) -> Any:
    return json.loads(json.dumps(trial))  # as the manifest file holds it


def test_every_drawn_scene_is_the_draw_of_its_own_seed() -> None:
    # Each scene is re-derived from its seed, written out as ADR 030 and ADR 026 name it, so a
    # mix-up of seeds in the generator fails here whichever seeds it swaps (#80 gate F1).
    sfp, sc = generate._bases(_sample())
    for split, name in (("tune", TUNE), ("heldout", HELDOUT)):
        committed = json.loads((EVAL_SIM / f"{name}.json").read_bytes())["scenes"]
        for family in FAMILIES:
            for i in range(4):
                drawn = generate._DRAW[family](random.Random(f"{split}:{family}:{i}"), sfp, sc)
                assert committed[f"{family}_{i}"]["trial"] == _plain(drawn), (split, family, i)
    pilot = json.loads((EVAL_SIM / f"{PILOT}.json").read_bytes())["scenes"]
    for family, draw, base in (
        ("inrange_sfp", generate._inrange_sfp, sfp),
        ("inrange_sc", generate._inrange_sc, sc),
    ):
        for i in range(4):
            drawn = draw(random.Random(f"pilot:{family}:{i}"), base)
            assert pilot[f"{family}_{i}"]["trial"] == _plain(drawn), (family, i)


def test_each_queue_order_is_the_shuffle_of_its_own_seed(by_name: dict[str, Split]) -> None:
    # ADR 030: random.Random("<split>:order"), A's order drawn first, then B's.
    queue = json.loads((EVAL_SIM / QUEUE).read_bytes())["splits"]
    for split, name in (("tune", TUNE), ("heldout", HELDOUT)):
        rng = random.Random(f"{split}:order")
        units = by_name[name].units()
        assert queue[name]["A"] == generate._shuffled(units, rng), split
        assert queue[name]["B"] == generate._shuffled(units, rng), split


def test_each_scene_of_a_family_draws_from_its_own_index() -> None:
    pairs = {"tune:tilt_sc:2": "tune:tilt_sc:3", "tune:tilt_sc:3": "tune:tilt_sc:2"}

    files = render(_sample(), EVAL_SIM / TEMPLATE, rng_for=_swapping(pairs))

    swapped = json.loads(files[f"{TUNE}.json"])["scenes"]
    committed = json.loads((EVAL_SIM / f"{TUNE}.json").read_bytes())["scenes"]
    assert swapped["tilt_sc_2"] == committed["tilt_sc_3"]
    assert swapped["tilt_sc_3"] == committed["tilt_sc_2"]
    assert swapped["tilt_sc_0"] == committed["tilt_sc_0"]


def test_a_template_over_its_bound_is_refused(tmp_path: Path) -> None:
    template = tmp_path / TEMPLATE
    template.write_bytes((EVAL_SIM / TEMPLATE).read_bytes() + b"#" * (1 << 20))

    with pytest.raises(EngineConfigError, match="over"):
        render(_sample(), template)


# --- inputs and the command ------------------------------------------------------------------


def test_a_base_scene_that_is_not_adr_026s_is_refused() -> None:
    sample = _sample()
    sample["trials"]["trial_1"]["tasks"]["task_1"]["port_name"] = "sfp_port_1"

    with pytest.raises(EngineConfigError, match="base scenes"):
        render(sample, EVAL_SIM / TEMPLATE)


def test_the_command_prints_why_a_sample_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = _sample()
    sample["trials"]["trial_3"]["tasks"]["task_1"]["target_module_name"] = "sc_port_0"
    path = tmp_path / "sample.yaml"
    path.write_text(yaml.safe_dump(sample), "utf-8")

    assert main(["--sample", str(path), "--out", str(_out(tmp_path))]) == 2

    assert "not the base scenes of ADR 026" in capsys.readouterr().err


def test_a_scene_the_validator_refuses_stops_the_generator() -> None:
    sample = _sample()
    sample["trials"]["trial_3"]["scene"]["task_board"]["pose"]["roll"] = 0.01

    with pytest.raises(EngineConfigError, match="flat"):
        render(sample, EVAL_SIM / TEMPLATE)


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "sim"
    out.mkdir()
    shutil.copy(EVAL_SIM / TEMPLATE, out / TEMPLATE)
    return out


def test_the_command_writes_the_files(tmp_path: Path, files: dict[str, bytes]) -> None:
    out = _out(tmp_path)

    assert main(["--sample", str(SAMPLE), "--out", str(out)]) == 0

    for name, data in files.items():
        assert (out / name).read_bytes() == data


def test_check_says_a_file_it_cannot_read_is_unreadable_not_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _out(tmp_path)
    main(["--sample", str(SAMPLE), "--out", str(out)])

    def locked(path: Path, data: bytes) -> bool:
        raise PermissionError("secret text from the OS")

    monkeypatch.setattr(generate, "_holds", locked)

    assert main(["--sample", str(SAMPLE), "--out", str(out), "--check"]) == 2
    err = capsys.readouterr().err
    assert "could not be read (PermissionError)" in err
    assert "differs" not in err and "secret" not in err


def test_check_passes_on_the_committed_files() -> None:
    assert main(["--sample", str(SAMPLE), "--out", str(EVAL_SIM), "--check"]) == 0


def test_check_names_a_file_that_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _out(tmp_path)
    main(["--sample", str(SAMPLE), "--out", str(out)])
    (out / "tune-v1.json").write_bytes((out / "tune-v1.json").read_bytes() + b" ")
    (out / QUEUE).unlink()

    assert main(["--sample", str(SAMPLE), "--out", str(out), "--check"]) == 1

    err = capsys.readouterr().err
    assert "tune-v1.json differs" in err and "queue.json differs" in err
    assert "pilot-v1.json" not in err


def test_an_unreadable_sample_prints_only_its_error_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = tmp_path / "sample.yaml"
    sample.write_text("trials: [secret text\n", "utf-8")

    assert main(["--sample", str(sample), "--out", str(_out(tmp_path))]) == 2

    err = capsys.readouterr().err
    assert "could not be read" in err and "secret" not in err
