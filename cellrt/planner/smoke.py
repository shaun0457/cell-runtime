"""One model call by hand, before an eval relies on the model (#49, ADR 017).

`run_smoke` sends one fixed, realistic failure through the model planner: the yaw-mismatch
UNKNOWN as the mock world produces it (phase0.yaml, seed 0), where switching to `insert_act` is
the answer that works. The report names the token budget and reasoning setting it asked with,
and says whether the answer came as a tool call or as text, whether it passed the planner
validation, and what it cost. It holds no provider text but the model id and the finish reason,
scrubbed and cut; a failed call shows its exception type only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from cellrt.planner.base import FailureContext, Traced
from cellrt.planner.llm import (
    ChatClient,
    ChatReply,
    ChatRequest,
    LlmPlanner,
    LlmPlannerConfig,
    Reasoning,
)
from cellrt.planner.schemas import Rejection, validate_decision
from cellrt.runtime.scrub import scrub
from cellrt.skills.mock import mock_registry
from cellrt.skills.registry import SkillRegistry

# A provider's model id and finish reason are cut to this in the report.
MAX_NAME_CHARS = 128
# The Monitor's evidence at the yaw mismatch's stall (phase0.yaml, seed 0, setting C).
YAW_EVIDENCE: Mapping[str, float] = {
    "force_n": 8.727,
    "progress_mm": 0.0879,
    "torque_nm": 0.664,
    "window_s": 1.0,
    "xy_error_mm": 0.220085,
}

Answer = Literal["tool_call", "text", "none", "error"]


def smoke_context(registry: SkillRegistry) -> FailureContext:
    """The yaw-mismatch UNKNOWN: insert_scripted stalls at the port's mouth under low force and
    some torque, with the plug centred. The skills come from `registry`."""
    names = sorted(registry.names())
    return FailureContext(
        skill="insert_scripted",
        attempt=1,
        failure_class="UNKNOWN",
        rule="insert_stalled",
        evidence=dict(YAW_EVIDENCE),
        time_left_s=52.8,
        planner_calls_left=2,
        recoveries_left=4,
        catalogue={n: registry.get(n).monitor_profile for n in names},
        options=("insert_act",),
        descriptions={n: str(getattr(registry.get(n), "description", "")) for n in names},
    )


@dataclass(frozen=True)
class SmokeReport:
    max_tokens: int  # the request's token budget, so saved reports can be told apart
    reasoning: Reasoning  # the request's reasoning setting
    answer: Answer  # how the model answered; "error" if the call raised
    valid: bool  # whether the answer passed the planner validation
    decision: str | None  # the decision's kind, when valid
    errors: tuple[str, ...]  # validation codes, when not valid
    error: str | None  # the exception type, if the call raised
    model: str | None  # as the provider names it
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    latency_s: float | None  # wall time of the call

    @classmethod
    def failed(cls, error: str, config: LlmPlannerConfig) -> SmokeReport:
        return cls(
            max_tokens=config.max_tokens,
            reasoning=config.reasoning,
            answer="error",
            valid=False,
            decision=None,
            errors=(),
            error=error,
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            finish_reason=None,
            latency_s=None,
        )

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["errors"] = list(self.errors)
        return data


def run_smoke(
    client: ChatClient,
    config: LlmPlannerConfig,
    *,
    registry: SkillRegistry | None = None,
    # The call's latency is wall time by design, as in the planner.
    timer: Callable[[], float] = time.perf_counter,  # noqa: TID251
) -> SmokeReport:
    """Send the smoke context once through `LlmPlanner(client, config)`; the whole call is cut
    at the config's deadline of wall time, as the runtime cuts it (ADR 016)."""
    registry = registry or mock_registry()
    return asyncio.run(_smoke(client, config, registry, timer))


async def _smoke(
    client: ChatClient,
    config: LlmPlannerConfig,
    registry: SkillRegistry,
    timer: Callable[[], float],
) -> SmokeReport:
    watched = _LastReply(client)
    ctx = smoke_context(registry)
    planner = LlmPlanner(watched, config, timer=timer)
    try:
        # A model call is wall time by design: this is the one bound on it here.
        answer = await asyncio.wait_for(planner.triage(ctx), config.deadline_s)  # noqa: TID251
    except Exception as exc:  # the type only: the text may hold anything
        return SmokeReport.failed(type(exc).__name__, config)
    assert isinstance(answer, Traced) and watched.reply is not None
    reply = watched.reply
    result = validate_decision(answer.raw, registry, ctx.skill, ctx.options)
    valid = not isinstance(result, Rejection)
    return SmokeReport(
        max_tokens=config.max_tokens,
        reasoning=config.reasoning,
        answer=_answer_kind(reply),
        valid=valid,
        decision=None if isinstance(result, Rejection) else result.kind,
        errors=result.errors if isinstance(result, Rejection) else (),
        error=None,
        model=_name(reply.model),
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        finish_reason=_name(reply.finish_reason),
        latency_s=answer.trace.latency_s,
    )


class _LastReply:
    """Passes calls through to a client and keeps the last reply, to see how the model
    answered (the planner's raw answer does not say)."""

    def __init__(self, client: ChatClient) -> None:
        self._client = client
        self.reply: ChatReply | None = None

    def describe(self) -> Mapping[str, str]:
        return self._client.describe()

    async def complete(self, request: ChatRequest) -> ChatReply:
        self.reply = await self._client.complete(request)
        return self.reply


def _answer_kind(reply: ChatReply) -> Answer:
    if reply.tool_arguments is not None:
        return "tool_call"
    return "text" if reply.text else "none"


def _name(value: object) -> str | None:
    return None if value is None else scrub(str(value))[:MAX_NAME_CHARS]
