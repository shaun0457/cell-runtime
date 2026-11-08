"""Event types. Every step of a run is one append-only event; state is a fold over them."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cellrt.runtime.task import Task

Outcome = Literal["success", "failed", "aborted"]

# The version of the event payloads a run was written with, recorded on `run_started`. Version 1
# is every run written before the field existed (`parse_stored` reads those as 1); 2 added the
# field itself; 3 added `signal_sampled.force_over_limit_s`; 4 added
# `run_started.geometry_source`; 5 added `hold_checked`. Bump it when a payload changes shape
# (a test hashes every payload's schema per version), and keep older rows readable: new fields
# get defaults.
SCHEMA_VERSION = 5


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunStarted(_Payload):
    type: Literal["run_started"] = "run_started"
    scenario_id: str
    setting: str
    seed: int
    attempt: int
    task: Task
    config_hash: str
    # A hash per part of the configuration (scenario, setting, world, monitor, skills, code,
    # adapters, lock, recovery, planner), so a log shows which parts two runs share;
    # config_hash covers them all.
    config_parts: dict[str, str] = Field(default_factory=dict)
    # Required, so no writer can forget it; rows stored before it existed get 1 on reading
    # (`parse_stored`).
    schema_version: int
    # Where skills got the plug and port poses: "ground_truth" is TF from the AIC sim (blocked in
    # the submission environment); None is the mock world, which has no poses.
    geometry_source: Literal["ground_truth"] | None = None


class FaultInjected(_Payload):
    """Ground truth for evaluation. Never read by skills or the Monitor."""

    type: Literal["fault_injected"] = "fault_injected"
    fault_id: str
    kind: str
    label: str
    params: dict[str, Any] = Field(default_factory=dict)


class FaultCleared(_Payload):
    type: Literal["fault_cleared"] = "fault_cleared"
    fault_id: str
    kind: str
    label: str
    params: dict[str, Any] = Field(default_factory=dict)


class SignalSampled(_Payload):
    type: Literal["signal_sampled"] = "signal_sampled"
    step: int
    height_mm: float
    depth_mm: float
    # What Align corrects on. None in logs written before it was recorded: a new field gets a
    # default, so every older log still reads (ADR 004).
    xy_offset_mm: tuple[float, float] | None = None
    xy_error_mm: float  # the magnitude, as the Monitor compared it
    raw_force_n: tuple[float, float, float]
    tare_offset_n: tuple[float, float, float]
    force_n: float  # tared, as the Monitor saw it
    torque_nm: float
    wrench_valid: bool
    # Time over the force limit within this sample's step, when the world reads the F/T stream
    # faster than it samples (AIC); None in the mock and in older logs (the step counts whole).
    # Never negative or NaN: either would make the live fold and its replay disagree.
    force_over_limit_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class SkillStarted(_Payload):
    type: Literal["skill_started"] = "skill_started"
    skill: str
    attempt: int
    params: dict[str, Any] = Field(default_factory=dict)


class PreconditionFailed(_Payload):
    """The skill was due to start but its preconditions did not hold, so it never ran."""

    type: Literal["precondition_failed"] = "precondition_failed"
    skill: str
    attempt: int


class SkillProgress(_Payload):
    type: Literal["skill_progress"] = "skill_progress"
    skill: str
    fraction: float
    detail: dict[str, Any] = Field(default_factory=dict)


class SkillFinished(_Payload):
    type: Literal["skill_finished"] = "skill_finished"
    skill: str
    ok: bool
    failure: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SkillCancelled(_Payload):
    type: Literal["skill_cancelled"] = "skill_cancelled"
    skill: str
    reason: Literal["terminated", "timeout", "preempted"]


class CommandDropped(_Payload):
    """A skill commanded the world after the executor had revoked its access (it had finished
    or been stopped). Only the first such command per skill run is recorded; `attempt` names
    the run that sent it, which may no longer be the latest run of that skill."""

    type: Literal["command_dropped"] = "command_dropped"
    skill: str
    attempt: int


class MonitorVerdict(_Payload):
    type: Literal["monitor_verdict"] = "monitor_verdict"
    skill: str
    failure_class: str
    rule: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class RecoveryStarted(_Payload):
    """The tree starts a recovery for a known failure class, within its budgets."""

    type: Literal["recovery_started"] = "recovery_started"
    skill: str
    failure_class: str
    action: str
    count_for_class: int
    count_total: int
    # Who classified the failure: the Monitor, or the skill itself when the Monitor gave no verdict.
    source: Literal["monitor", "skill"] = "monitor"


class PlannerCalled(_Payload):
    """Setting C asks the planner about a failure (see `planner.base.FailureContext`)."""

    type: Literal["planner_called"] = "planner_called"
    skill: str
    failure_class: str
    call: int  # 1 for the task's first planner call
    context: dict[str, Any] = Field(default_factory=dict)


class PlannerDecided(_Payload):
    """The planner's output passed validation."""

    type: Literal["planner_decided"] = "planner_decided"
    skill: str
    decision: dict[str, Any]


class PlannerOutputRejected(_Payload):
    """The planner's output failed validation; nothing it asked for runs."""

    type: Literal["planner_output_rejected"] = "planner_output_rejected"
    skill: str
    raw: str  # cut to planner.schemas.MAX_RAW_CHARS
    errors: list[str]


class BtNodeChanged(_Payload):
    type: Literal["bt_node_changed"] = "bt_node_changed"
    node: str
    status: str


class RunFinished(_Payload):
    type: Literal["run_finished"] = "run_finished"
    outcome: Outcome
    reason: str
    steps: int
    duration: float


class HoldChecked(_Payload):
    """Whether the arm came to rest after the run's last HOLD: the linear TCP speed under the
    threshold for enough consecutive samples before the deadline (a world that measures speed;
    the AIC cell). Recorded just before `run_finished`."""

    type: Literal["hold_checked"] = "hold_checked"
    result: Literal["hold_confirmed", "hold_unconfirmed"]
    max_speed_mm_s: float = Field(ge=0, allow_inf_nan=False)  # the fastest sample seen
    samples: int = Field(ge=0)
    waited_s: float = Field(ge=0, allow_inf_nan=False)  # clock time the check took


Payload = Annotated[
    RunStarted
    | FaultInjected
    | FaultCleared
    | SignalSampled
    | SkillStarted
    | PreconditionFailed
    | SkillProgress
    | SkillFinished
    | SkillCancelled
    | CommandDropped
    | MonitorVerdict
    | RecoveryStarted
    | PlannerCalled
    | PlannerDecided
    | PlannerOutputRejected
    | BtNodeChanged
    | HoldChecked
    | RunFinished,
    Field(discriminator="type"),
]
PAYLOAD_ADAPTER: TypeAdapter[Payload] = TypeAdapter(Payload)


def parse_stored(event_type: str, raw: str) -> Payload:
    """A payload as stored in the event log. A `run_started` row written before the schema
    version existed has no `schema_version`; it reads as version 1."""
    if event_type != "run_started":
        return PAYLOAD_ADAPTER.validate_json(raw)
    data = json.loads(raw)
    if isinstance(data, dict):
        data.setdefault("schema_version", 1)
    return PAYLOAD_ADAPTER.validate_python(data)


class Event(BaseModel):
    """Envelope: where the event belongs (run, seq) and when it happened.

    `ts` is clock time (simulated in mock mode) and is the only time the fold uses. `wall_ts`
    is real time, kept for debugging and ignored by every determinism comparison.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    seq: int = Field(ge=1)
    ts: float
    wall_ts: float
    payload: Payload

    @property
    def type(self) -> str:
        return self.payload.type
