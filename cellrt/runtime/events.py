"""Event types. Every step of a run is one append-only event; state is a fold over them."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cellrt.runtime.task import Task

Outcome = Literal["success", "failed", "aborted"]


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
    xy_error_mm: float
    force_n: float
    torque_nm: float
    wrench_valid: bool


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
    reason: Literal["terminated", "timeout"]


class MonitorVerdict(_Payload):
    type: Literal["monitor_verdict"] = "monitor_verdict"
    skill: str
    failure_class: str
    rule: str
    evidence: dict[str, Any] = Field(default_factory=dict)


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
    | MonitorVerdict
    | BtNodeChanged
    | RunFinished,
    Field(discriminator="type"),
]
PAYLOAD_ADAPTER: TypeAdapter[Payload] = TypeAdapter(Payload)


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
