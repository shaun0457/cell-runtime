"""The manifest that names each goal's scene (ADR 027). ROS-free and pure.

aic_engine passes one field of our engine config through to the policy: the task id, the key of
the trial's `tasks:` block (aic_engine.cpp:224-248). The batch runner writes every task id as
`<scene id>_r<k>`, and a manifest maps it to its scene, its repeat k (the run's seed) and the
scene's digest. `RuntimePolicy` reads the manifest that `CELLRT_MANIFEST` names, once, and
names each goal's unit from it instead of counting goals. A goal whose task differs from its
scene's is refused, but a manifest that reuses a scene id for another scene with the same
task is not caught here: the runner renders the engine config from the very manifest it
names in `CELLRT_MANIFEST` (ADR 028), so the two cannot disagree.

A manifest is read strictly and with bounds (`MAX_BYTES`, `MAX_UNITS`): every field is checked,
each scene's digest must match its trial, and each task id must name its scene and repeat. A
refusal is one of the fixed messages below, raised with no cause attached, so nothing the file
holds reaches a log.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

MANIFEST_ENV = "CELLRT_MANIFEST"
MAX_BYTES = 1 << 20
MAX_UNITS = 2000

MANIFEST_UNREADABLE = "CELLRT_MANIFEST names no readable manifest file"
MANIFEST_TOO_LARGE = f"the manifest is over {MAX_BYTES} bytes"
MANIFEST_INVALID = "the manifest is not a valid scene manifest"
TASK_NOT_IN_MANIFEST = "the goal's task id is not in the manifest"
TASK_MISMATCH = "the goal's task differs from its scene's task in the manifest"

# The fields of a goal's task that its scene fixes (the trial's `tasks:` entry, without its id).
TASK_FIELDS = (
    "cable_type",
    "cable_name",
    "plug_type",
    "plug_name",
    "port_type",
    "port_name",
    "target_module_name",
    "time_limit",
)

TaskId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
SceneId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,56}$")]
Family = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]


class ManifestError(ValueError):
    """A manifest or a lookup is refused. The message is one of the fixed ones above."""


def scene_digest(trial: Mapping[str, Any]) -> str:
    """The SHA-256 of a scene's trial (its `scene` and `task` nodes, without the task id, so the
    repeats of one scene share it): compact JSON with sorted keys."""
    text = json.dumps(trial, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SceneEntry(_Strict):
    family: Family
    digest: Digest
    hypothesis: Annotated[str, StringConstraints(max_length=300)] = ""
    expected_class: Family = "unknown"
    trial: dict[str, Any]

    @model_validator(mode="after")
    def _digest_matches(self) -> SceneEntry:
        if scene_digest(self.trial) != self.digest:
            raise ValueError("the digest does not match the trial")
        return self


class UnitEntry(_Strict):
    scene: SceneId
    seed: Annotated[int, Field(ge=0, lt=MAX_UNITS)]


@dataclass(frozen=True)
class Placement:
    """Where a goal's run goes: its unit's scenario id and seed, and its scene's digest."""

    scenario_id: str
    seed: int
    scene_digest: str


class Manifest(_Strict):
    name: Name
    version: Literal[1]
    scenes: Annotated[dict[SceneId, SceneEntry], Field(min_length=1)]
    units: Annotated[dict[TaskId, UnitEntry], Field(min_length=1, max_length=MAX_UNITS)]

    @model_validator(mode="after")
    def _units_name_their_scenes(self) -> Manifest:
        for task_id, unit in self.units.items():
            if unit.scene not in self.scenes or task_id != f"{unit.scene}_r{unit.seed}":
                raise ValueError("a task id does not name a scene of the manifest and its repeat")
        return self

    def lookup(self, task_id: str, task: Mapping[str, Any] | None = None) -> Placement:
        """The goal's placement; ManifestError(TASK_NOT_IN_MANIFEST) for an unknown task id.
        Given the goal's `task` fields, also ManifestError(TASK_MISMATCH) when any of
        `TASK_FIELDS` differs from the scene's task: a stale manifest that reuses a task id for
        another task cannot put its digest on the run. One that reuses it for another scene with
        the same task is not caught here; the runner rules it out (ADR 027, ADR 028)."""
        unit = self.units.get(task_id)
        if unit is None:
            raise ManifestError(TASK_NOT_IN_MANIFEST)
        scene = self.scenes[unit.scene]
        if task is not None and not _same_task(scene.trial.get("task"), task):
            raise ManifestError(TASK_MISMATCH)
        return Placement(
            scenario_id=f"{self.name}/{unit.scene}",
            seed=unit.seed,
            scene_digest=scene.digest,
        )


def _same_task(expected: object, goal: Mapping[str, Any]) -> bool:
    if not isinstance(expected, Mapping):
        return False
    for key in TASK_FIELDS:
        want, got = expected.get(key), goal.get(key)
        if key == "time_limit":  # whole seconds in the config, a float in the core `Task`
            numbers = all(
                isinstance(v, int | float) and not isinstance(v, bool) for v in (want, got)
            )
            if not numbers or float(want) != float(got):  # type: ignore[arg-type]
                return False
        elif not isinstance(want, str) or want != got:
            return False
    return True


def load_manifest(path: Path) -> Manifest:
    """The manifest at `path`, checked; ManifestError with a fixed message otherwise."""
    try:
        # A FIFO with no writer would block `open` in aic_model's action thread: only a regular
        # file is opened. Then at most one byte past the bound is read, so a file that grows
        # cannot make the read unbounded either.
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise ManifestError(MANIFEST_UNREADABLE)
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except OSError:
        raise ManifestError(MANIFEST_UNREADABLE) from None
    if len(raw) > MAX_BYTES:
        raise ManifestError(MANIFEST_TOO_LARGE)
    try:
        return Manifest.model_validate(json.loads(raw.decode("utf-8")))
    except (ValueError, ValidationError, RecursionError):  # their text may quote the file
        raise ManifestError(MANIFEST_INVALID) from None
