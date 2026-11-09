"""A planner that asks a language model, through a chat client the core defines.

The core never imports a vendor SDK: `ChatClient` is the seam, and a provider's client (the NIM
one, #49) or a test's scripted client sits behind it. The model's answer is raw and untrusted:
it goes back to the executor as a `Traced` answer and through the same validation as every
planner's output (ADR 008). The trace (prompt, raw answer, model, tokens, latency) goes to the
event log, so replay never needs the model.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from cellrt.planner.base import FailureContext, PlannerTrace, Traced
from cellrt.planner.schemas import audit_text, decision_json_schema

MAX_MODEL_CHARS = 128
MAX_TOKENS_LIMIT = 8_192
MAX_TEMPERATURE = 2.0
TOOL_NAME = "decide"

_ROLE = """\
You are the recovery planner of a robotic cable-insertion cell. A skill has failed, and the
runtime's Monitor could not classify the failure (UNKNOWN). The arm is holding still and waits
for your decision. You never control motion, speeds, forces or thresholds: you only choose what
the runtime does next.

The user message is the failure context as JSON: the failed skill and its attempt, the Monitor
rule that fired and its evidence (force in N, torque in Nm, depth and progress in mm, XY error
in mm), the time, planner calls and recoveries left, the step's options, and what each
registered skill does.

Choose exactly one decision:
- switch_skill: run another skill in place of the failed one. Name one of `options`, never the
  failed skill itself.
- retry: run the failed skill again.
- abort: give up on the task, with a short reason.

Read the evidence and the skill descriptions. Prefer the action most likely to succeed. If no
option is likely to succeed, abort early rather than spend the remaining budget.
"""
_TOOL_INSTRUCTION = f"Answer by calling the `{TOOL_NAME}` tool exactly once."
_JSON_INSTRUCTION = (
    "Answer with one JSON object and nothing else. It must match this JSON schema:\n"
    + json.dumps(decision_json_schema(), sort_keys=True)
)
SYSTEM_PROMPTS: Mapping[str, str] = {
    "tool": f"{_ROLE}\n{_TOOL_INSTRUCTION}",
    "json": f"{_ROLE}\n{_JSON_INSTRUCTION}",
}
DECIDE_TOOL: Mapping[str, object] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "The runtime's next action for the failed step.",
        "parameters": decision_json_schema(),
    },
}
# Changes whenever the prompts or the tool change, so two prompt versions never share a hash.
PROMPT_VERSION = hashlib.sha256(
    json.dumps([SYSTEM_PROMPTS, DECIDE_TOOL], sort_keys=True).encode("utf-8")
).hexdigest()[:12]


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: Sequence[Mapping[str, str]]  # {"role": ..., "content": ...}
    tool: Mapping[str, object] | None  # one function tool, or None for a JSON answer
    max_tokens: int
    temperature: float


@dataclass(frozen=True)
class ChatReply:
    model: str  # as the provider names it
    tool_arguments: str | None  # the tool call's arguments, as sent
    text: str | None  # the message text
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None


class ChatClient(Protocol):
    """One chat completion against some provider. Implementations hold their own credentials
    and must never put them in what `describe` returns."""

    def describe(self) -> Mapping[str, str]:
        """Who answers: at least `provider`; a remote client adds its host. Hashed into the
        run's configuration."""
        ...

    async def complete(self, request: ChatRequest) -> ChatReply: ...


@dataclass(frozen=True)
class LlmPlannerConfig:
    model: str
    max_tokens: int = 1_024
    temperature: float = 0.0
    mode: Literal["tool", "json"] = "tool"

    def __post_init__(self) -> None:
        if not 0 < len(self.model) <= MAX_MODEL_CHARS:
            raise ValueError(f"model must be 1 to {MAX_MODEL_CHARS} characters")
        if not 0 < self.max_tokens <= MAX_TOKENS_LIMIT:
            raise ValueError(f"max_tokens must be in (0, {MAX_TOKENS_LIMIT}]")
        if not 0 <= self.temperature <= MAX_TEMPERATURE:
            raise ValueError(f"temperature must be in [0, {MAX_TEMPERATURE}]")
        if self.mode not in SYSTEM_PROMPTS:
            raise ValueError(f"mode must be one of {', '.join(SYSTEM_PROMPTS)}")


class LlmPlanner:
    """Triage by a model: one chat call per failure, answered through the `decide` tool (or as
    JSON text in "json" mode)."""

    def __init__(
        self,
        client: ChatClient,
        config: LlmPlannerConfig,
        *,
        # A model call's latency is wall time by design: recorded in the trace, never folded.
        timer: Callable[[], float] = time.perf_counter,  # noqa: TID251
    ) -> None:
        self._client = client
        self.config = config
        self._timer = timer  # wall time, for the call's latency

    def describe(self) -> Mapping[str, object]:
        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "mode": self.config.mode,
            "prompt_version": PROMPT_VERSION,
            "client": dict(self._client.describe()),
        }

    async def triage(self, ctx: FailureContext) -> object:
        messages = (
            {"role": "system", "content": SYSTEM_PROMPTS[self.config.mode]},
            {"role": "user", "content": json.dumps(ctx.as_dict(), sort_keys=True)},
        )
        request = ChatRequest(
            model=self.config.model,
            messages=messages,
            tool=DECIDE_TOOL if self.config.mode == "tool" else None,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )
        started = self._timer()
        reply = await self._client.complete(request)
        latency_s = max(0.0, self._timer() - started)
        raw = reply.tool_arguments if reply.tool_arguments is not None else reply.text or ""
        # A client that already parsed the arguments hands over a structure: it is validated as
        # it is, and recorded as bounded text.
        response = raw if isinstance(raw, str) else audit_text(raw)
        trace = PlannerTrace(
            provider=str(self._client.describe().get("provider", "unknown")),
            model=reply.model or self.config.model,
            prompt=json.dumps(messages),
            response=response,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            finish_reason=reply.finish_reason,
            latency_s=latency_s,
        )
        return Traced(raw=raw, trace=trace)
