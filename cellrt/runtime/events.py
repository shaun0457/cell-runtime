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
# `run_started.geometry_source`; 5 added `hold_checked`; 6 added
# `signal_sampled.wrench_dropped`; 7 added `planner_responded`; 8 added `planner_timed_out`;
# 9 added `planner_preempted`; 10 added `planner_decided.waited_sim_s`; 11 added `task_planned`
# and `task_plan_rejected`; 12 added `task_plan_responded`; 13 added `promoted_stopped`.
# Bump it when a payload changes shape (a test hashes every payload's schema per version), and
# keep older rows readable: new fields get defaults.
SCHEMA_VERSION = 13


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


class TaskPlanResponded(_Payload):
    """A model task planner's answer as it came back, before it was parsed and checked: the
    audit trail of the run's one plan call (ADR 023). Recorded just before `task_planned` or
    `task_plan_rejected`, only for a task planner that reports a trace. Kept apart from
    `planner_responded`, which is an UNKNOWN triage call. Scrubbed and cut like it
    (`PlannerTrace.stored`)."""

    type: Literal["task_plan_responded"] = "task_plan_responded"
    planner: str
    provider: str
    model: str
    prompt: str  # the messages sent, as JSON
    response: str  # the tool call's arguments, or the text
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    latency_s: float = Field(ge=0, allow_inf_nan=False)  # wall time the call took


class TaskPlanned(_Payload):
    """The task planner's plan passed the verifier (ADR 023): the tree runs these steps, each in
    the setting's recovery. Recorded only when the tree is not the fixed one, so other runs keep
    their shape."""

    type: Literal["task_planned"] = "task_planned"
    planner: str
    steps: list[str]


class TaskPlanRejected(_Payload):
    """The task planner's plan failed the verifier, or the planner raised: no tree is built and
    the run ends aborted before any motion (ADR 023)."""

    type: Literal["task_plan_rejected"] = "task_plan_rejected"
    planner: str
    # As proposed, at most planner.task_plan.MAX_PLAN_STEPS + 1 names, each scrubbed and cut to
    # MAX_STEP_CHARS; empty if the planner raised.
    steps: list[str]
    errors: list[str]  # codes ("step 2: needs_unmet"), never a planner's text


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
    # F/T readings the world had to drop in this step because its buffer was full (AIC); None
    # in the mock and in older logs.
    wrench_dropped: int | None = Field(default=None, ge=0)


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
    """The tree starts a recovery, within its budgets: a known class's plan, a planner's
    decision, or an approved promoted rule's plan (`action` `promoted:<NAME>:...`)."""

    type: Literal["recovery_started"] = "recovery_started"
    skill: str
    failure_class: str
    action: str
    count_for_class: int
    count_total: int
    # Who classified the failure: the Monitor, or the skill itself when the Monitor gave no verdict.
    source: Literal["monitor", "skill"] = "monitor"


class PromotedStopped(_Payload):
    """An approved promoted rule's plan (ADR 021) stopped the run instead of recovering: `abort`,
    or `escalate` to a person. No recovery starts and no budget is spent; the run ends aborted,
    as a planner's abort or escalation does. The verdict naming the rule comes before it."""

    type: Literal["promoted_stopped"] = "promoted_stopped"
    skill: str
    rule: str  # the promoted rule's name, without the `promoted:` prefix
    action: Literal["abort", "escalate"]


class PlannerCalled(_Payload):
    """Setting C asks the planner about a failure (see `planner.base.FailureContext`)."""

    type: Literal["planner_called"] = "planner_called"
    skill: str
    failure_class: str
    call: int  # 1 for the task's first planner call
    context: dict[str, Any] = Field(default_factory=dict)


class PlannerResponded(_Payload):
    """A model planner's answer as it came back, before validation: the audit trail of one call.

    Recorded only for planners that report a trace (a model planner), so runs with the rule
    planner keep their shape. The prompt is our own text; the response came from outside and is
    scrubbed. Both are cut to fixed lengths (`planner.base`).
    """

    type: Literal["planner_responded"] = "planner_responded"
    skill: str
    provider: str
    model: str
    prompt: str  # the messages sent, as JSON
    response: str  # the tool call's arguments, or the text
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    latency_s: float = Field(ge=0, allow_inf_nan=False)  # wall time the call took


class PlannerTimedOut(_Payload):
    """The planner did not answer within its wall-time deadline: the call was cancelled, and
    any later answer is never read. The step fails without running anything."""

    type: Literal["planner_timed_out"] = "planner_timed_out"
    skill: str
    deadline_s: float = Field(gt=0, allow_inf_nan=False)
    waited_s: float = Field(ge=0, allow_inf_nan=False)  # wall time, from when the loop waited


class PlannerPreempted(_Payload):
    """A Monitor verdict during a live wait (ADR 020) cut the call short: it was cancelled,
    any answer is never read, and the step recovers as if its skill had failed with the
    verdict's class. The verdict itself is the `monitor_verdict` just before."""

    type: Literal["planner_preempted"] = "planner_preempted"
    skill: str
    failure_class: str
    # wall time since the loop first held for the call (0 if it never did)
    waited_s: float = Field(ge=0, allow_inf_nan=False)
    waited_sim_s: float = Field(ge=0, allow_inf_nan=False)  # clock time since the call


class PlannerDecided(_Payload):
    """The planner's output passed validation."""

    type: Literal["planner_decided"] = "planner_decided"
    skill: str
    decision: dict[str, Any]
    # Clock time between the call and the answer being read: 0 for a call the loop waited on
    # in `frozen` (and in rows from before schema 10), one period for an answer in the
    # call's first step, which is read on the next tick.
    waited_sim_s: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class PlannerOutputRejected(_Payload):
    """The planner's output failed validation, or a valid answer arrived too late to apply (the
    code `skill: preconditions_not_met`, ADR 020); nothing it asked for runs."""

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
    | TaskPlanResponded
    | TaskPlanned
    | TaskPlanRejected
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
    | PromotedStopped
    | PlannerCalled
    | PlannerResponded
    | PlannerTimedOut
    | PlannerPreempted
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
