"""A model as the task planner (ADR 023): one chat call per plan, before the tree is built.

The model is told the task and the skills it may plan (each with what it does and its step
contract) and answers through the `plan` tool. Its answer is raw and untrusted: `plan_task`
parses it into `TaskPlan` and checks it like any plan, and a rejected plan ends the run before
any motion. The call's trace goes to the event log (`task_plan_responded`), so replay never
needs the model. The model knows only what the prompt says: nothing about the plug beyond the
task's fields, and nothing about the scenario's faults.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping

from cellrt.planner.base import MAX_DESCRIPTION_CHARS, PlannerTrace
from cellrt.planner.llm import ChatClient, ChatRequest, LlmPlannerConfig
from cellrt.planner.schemas import audit_text
from cellrt.planner.task_plan import GOAL, MAX_PLAN_STEPS, STEP_CONTRACTS, TaskPlan, TracedPlan
from cellrt.runtime.task import Task
from cellrt.skills.registry import SkillRegistry

TOOL_NAME = "plan"
# A skill's description is cut to this, as in the UNKNOWN planner's context.
MAX_SKILL_DESCRIPTION_CHARS = MAX_DESCRIPTION_CHARS

_ROLE = """\
You are the task planner of a robotic cable-insertion cell. Before the arm moves, you choose
which skills the task runs, and in what order. You never control motion, speeds, forces or
thresholds, and you do not plan recovery: when a step fails, the runtime retries it, re-aligns
or switches skills on its own.

The user message is the task as JSON: the cable, plug and port types and names, the target
module, the time limit in seconds, the goal fact, the most steps a plan may have, and the skills
you may plan. Each skill says what it does, the fact it needs before it can run (null: it can
run first) and the fact it gives.

A plan is valid when every step is one of those skills, each step's need was given by an
earlier step, no fact is given twice, and the goal is given. Choose the skills most likely to
complete this task.
"""
SYSTEM_PROMPT = f"{_ROLE}\nAnswer by calling the `{TOOL_NAME}` tool exactly once."


def _plan_parameters() -> dict[str, object]:
    """The `plan` tool's parameters without the skill names: `TaskPlan`'s JSON schema."""
    schema = TaskPlan.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


def plannable(registry: SkillRegistry) -> tuple[str, ...]:
    """The registered skills that have a step contract, sorted: what the model may plan.
    Recovery helpers (`retract`, `wait_sensor`) have none and are not offered."""
    return tuple(sorted(name for name in STEP_CONTRACTS if name in registry))


def plan_tool(names: tuple[str, ...]) -> Mapping[str, object]:
    """The `plan` tool: `TaskPlan`'s schema, with each step limited to `names`."""
    parameters = _plan_parameters()
    steps = dict(parameters["properties"]["steps"])  # type: ignore[index]
    steps["items"] = {"type": "string", "enum": list(names)}
    parameters["properties"] = {"steps": steps}
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "The task's steps, in order.",
            "parameters": parameters,
        },
    }


def task_context(task: Task, descriptions: Mapping[str, str]) -> dict[str, object]:
    """What the model is told: the task, the goal, the step limit and each plannable skill
    (`descriptions` maps each one to what its skill says it does)."""
    skills: dict[str, object] = {}
    for name, does in sorted(descriptions.items()):
        contract = STEP_CONTRACTS[name]
        skills[name] = {
            "does": does[:MAX_SKILL_DESCRIPTION_CHARS],
            "needs": contract.needs,
            "gives": contract.gives,
        }
    return {
        "task": task.model_dump(mode="json"),
        "goal": GOAL,
        "max_steps": MAX_PLAN_STEPS,
        "skills": skills,
    }


# Changes whenever the system prompt, the tool or the user message's layout changes (one
# example of each is hashed, a description longer than the cut included), so two prompt
# versions never share a hash. The names and descriptions themselves come from the registry:
# a description is a constant in its skill's source, which the `code` and `adapters` parts
# hash (the `skills` part holds a skill's parameters, not its description).
PROMPT_VERSION = hashlib.sha256(
    json.dumps(
        [
            SYSTEM_PROMPT,
            plan_tool(("verify",)),
            task_context(Task(), {"verify": "x" * (MAX_SKILL_DESCRIPTION_CHARS + 1)}),
        ],
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:12]


class LlmTaskPlanner:
    """Plans a task by one model call, answered through the `plan` tool. Its calls count against
    the cap its client holds, like the UNKNOWN planner's."""

    def __init__(
        self,
        client: ChatClient,
        config: LlmPlannerConfig,
        *,
        # A model call's latency is wall time by design: recorded in the trace, never folded.
        timer: Callable[[], float] = time.perf_counter,  # noqa: TID251
    ) -> None:
        if config.mode != "tool":
            # One answer format keeps one prompt version; the tool also limits each step to a
            # plannable name, which a JSON answer would only be told.
            raise ValueError("the task planner answers through its tool: mode must be 'tool'")
        self._client = client
        self.config = config
        self._timer = timer

    @property
    def name(self) -> str:
        return "llm"

    @property
    def deadline_s(self) -> float:
        return self.config.deadline_s

    def describe(self) -> Mapping[str, object]:
        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "deadline_s": self.config.deadline_s,
            "reasoning": self.config.reasoning,
            "prompt_version": PROMPT_VERSION,
            "client": dict(self._client.describe()),
        }

    async def plan(self, task: Task, registry: SkillRegistry) -> TracedPlan:
        names = plannable(registry)
        descriptions = {n: str(getattr(registry.get(n), "description", "")) for n in names}
        messages = (
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(task_context(task, descriptions), sort_keys=True),
            },
        )
        request = ChatRequest(
            model=self.config.model,
            messages=messages,
            tool=plan_tool(names),
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            reasoning=self.config.reasoning,
        )
        started = self._timer()
        reply = await self._client.complete(request)
        latency_s = max(0.0, self._timer() - started)
        raw = reply.tool_arguments if reply.tool_arguments is not None else reply.text or ""
        # A client that already parsed the arguments hands over a structure: it is parsed as it
        # is, and recorded as bounded text.
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
        return TracedPlan(raw=raw, trace=trace)
