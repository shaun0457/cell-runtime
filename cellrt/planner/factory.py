"""The planner setting C asks, built from command-line settings, and a command's call pre-check.

`build_planner(kind)` builds "rule" (Phase 0's rule planner), "fixed" (the retry-then-abort
policy, ADR 019) or "llm", the model planner on NIM (ADR 017); `build_llm_task_planner` builds
the model task planner (ADR 023) on the same client. Every setup error is an
`LlmSetupError` with a fixed message, safe to print. A model planner's calls count against the
cap it is given: the command makes one cap and gives it to every planner it builds.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from cellrt.planner.base import DEFAULT_PLANNER_DEADLINE_S, Planner
from cellrt.planner.fixed_planner import FixedPlanner
from cellrt.planner.llm import DEFAULT_MAX_TOKENS, LlmPlanner, LlmPlannerConfig, Reasoning
from cellrt.planner.llm_task import LlmTaskPlanner
from cellrt.planner.nim import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MAX_MODEL_CALLS_LIMIT,
    Connect,
    LlmSetupError,
    ModelCallCap,
    NimChatClient,
    ToolChoice,
    connect_openai,
    nim_client,
)
from cellrt.planner.rule_planner import RulePlanner

PLANNER_KINDS: tuple[str, ...] = ("rule", "fixed", "llm")
DEFAULT_MAX_MODEL_CALLS = 100


class PlannerSettingRefused(LlmSetupError, ValueError):
    """A planner setting out of range. The message is one of this package's fixed texts."""


class CallBudgetExceeded(LlmSetupError):
    """The command could make more model calls than its cap allows; it does not start."""


def model_call_cap(max_model_calls: int) -> ModelCallCap:
    """The cap for one command's model calls, refused with a fixed message when out of range."""
    try:
        return ModelCallCap(max_model_calls)
    except ValueError as exc:
        raise PlannerSettingRefused(str(exc)) from None


def check_call_budget(
    units_with_planner: int, planner_budget: int, max_model_calls: int, *, plan_calls: int = 0
) -> int:
    """The most model calls the command can make: units that ask the planner, times the
    per-task planner budget, plus `plan_calls`, the model task planner's calls (ADR 023): one
    per scenario and seed in an eval, whose settings share the plan, and one for `cellrt run`.
    Refused before the command starts when that exceeds the cap."""
    for value in (units_with_planner, planner_budget, max_model_calls, plan_calls):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PlannerSettingRefused("unit, budget and cap counts must be non-negative integers")
    if max_model_calls > MAX_MODEL_CALLS_LIMIT:
        raise PlannerSettingRefused(f"the model call cap must be at most {MAX_MODEL_CALLS_LIMIT}")
    needed = units_with_planner * planner_budget + plan_calls
    if needed > max_model_calls:
        parts: list[str] = []
        if units_with_planner:
            parts.append(
                f"{units_with_planner} units with a planner x {planner_budget} planner calls each"
            )
        if plan_calls:
            parts.append(f"{plan_calls} task plan calls")
        raise CallBudgetExceeded(
            f"this command may make up to {needed} model calls ({' plus '.join(parts)}), more "
            f"than the cap of {max_model_calls}: raise --max-model-calls or run fewer units"
        )
    return needed


def build_planner(
    kind: str,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
    mode: Literal["tool", "json"] = "tool",
    deadline_s: float = DEFAULT_PLANNER_DEADLINE_S,
    tool_choice: ToolChoice = "auto",
    reasoning: Reasoning = "default",
    cap: ModelCallCap | None = None,
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
    env: Mapping[str, str] | None = None,
    connect: Connect = connect_openai,
) -> Planner:
    """The planner of `kind`. The model settings are used by "llm" only. Its calls count against
    `cap` (a new cap of `max_model_calls` if none is given). The deadline is one value: the
    runtime cancels the call at it (ADR 016) and the SDK's timeout is set to it. The key comes
    from `env` (the process environment by default) and goes nowhere but the SDK."""
    if kind == "rule":
        return RulePlanner()
    if kind == "fixed":
        return FixedPlanner()
    if kind != "llm":
        raise PlannerSettingRefused(f"the planner must be one of: {', '.join(PLANNER_KINDS)}")
    client, config = llm_client_and_config(
        model=model,
        base_url=base_url,
        max_tokens=max_tokens,
        temperature=temperature,
        mode=mode,
        deadline_s=deadline_s,
        tool_choice=tool_choice,
        reasoning=reasoning,
        cap=cap if cap is not None else model_call_cap(max_model_calls),
        env=env,
        connect=connect,
    )
    return LlmPlanner(client, config)


def build_llm_task_planner(
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    reasoning: Reasoning = "default",
    cap: ModelCallCap,
    env: Mapping[str, str] | None = None,
    connect: Connect = connect_openai,
) -> LlmTaskPlanner:
    """The model task planner on NIM (ADR 023): the model planner's default settings but the
    model, the token budget and the reasoning, which a command sets for both. It makes one call
    per plan asked for (once per scenario and seed in an eval, once per `cellrt run`), each
    counted against `cap`, the same cap as the UNKNOWN planner's calls."""
    client, config = llm_client_and_config(
        model=model, max_tokens=max_tokens, reasoning=reasoning, cap=cap, env=env, connect=connect
    )
    return LlmTaskPlanner(client, config)


def llm_client_and_config(
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
    mode: Literal["tool", "json"] = "tool",
    deadline_s: float = DEFAULT_PLANNER_DEADLINE_S,
    tool_choice: ToolChoice = "auto",
    reasoning: Reasoning = "default",
    cap: ModelCallCap,
    env: Mapping[str, str] | None = None,
    connect: Connect = connect_openai,
) -> tuple[NimChatClient, LlmPlannerConfig]:
    """The model planner's two parts, checked: the NIM client and the planner's config. The
    smoke call takes them apart; `build_planner` puts them together."""
    try:
        config = LlmPlannerConfig(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            mode=mode,
            deadline_s=deadline_s,
            reasoning=reasoning,
        )
        client = nim_client(
            cap=cap,
            base_url=base_url,
            timeout_s=config.deadline_s,
            tool_choice=tool_choice,
            env=env,
            connect=connect,
        )
    except LlmSetupError:
        raise
    except ValueError as exc:  # LlmPlannerConfig's and the client's checks: fixed messages
        raise PlannerSettingRefused(str(exc)) from None
    return client, config
