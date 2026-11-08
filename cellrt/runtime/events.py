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
    raw_force_n: tuple[float, float, float]
    tare_offset_n: tuple[float, float, float]
    force_n: float  # tared, as the Monitor saw it
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
    | CommandDropped
    | MonitorVerdict
    | RecoveryStarted
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
