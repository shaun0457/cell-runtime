"""Ask a model for a candidate rule for one UNKNOWN group (ADR 021, ticket T4).

One chat call through `ChatClient`, the planner's seam: the group's evidence, the rule language
and the registered insertion skills go in, and the answer must be a `Candidate`. It goes through
the same parser and registry check as a hand-written one, and is never adopted from here: it
still has to pass the replay gates (`cellrt promote validate`) and a person's approval.

What the model sees is evidence only: the verdicts' readings and the signatures of the other
groups. Run ids and the injected faults' labels are referee data (ADR 015) and stay out.

The proposal record keeps the prompt and the raw answer (scrubbed and cut), the model, tokens,
latency and the outcome. A refusal is a code, never the model's text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, get_args

from cellrt.executor.behaviours import MAX_DESCRIPTION_CHARS
from cellrt.monitor.classifier import MonitorConfig
from cellrt.monitor.promoted import (
    FIELD_BOUNDS,
    MAX_CONDITIONS,
    MAX_WINDOW_S,
    MIN_WINDOW_S,
    NAME_PATTERN,
    Candidate,
    CandidateRefused,
    Condition,
    Op,
    PromotedPlan,
    PromotedRule,
)
from cellrt.planner.base import MAX_STORED_PROMPT_CHARS, MAX_STORED_RESPONSE_CHARS
from cellrt.planner.llm import ChatClient, ChatReply, ChatRequest, LlmPlannerConfig
from cellrt.planner.nim import ModelCallCapReached
from cellrt.promotion.groups import Group
from cellrt.promotion.validate import parse_checked
from cellrt.runtime.scrub import scrub
from cellrt.skills.registry import SkillRegistry

PROPOSAL_VERSION = 1
TOOL_NAME = "propose"
# A rule and its plan are a few hundred tokens; a reasoning model spends more before it answers.
PROPOSE_MAX_TOKENS = 4_096
PROPOSE_DEADLINE_S = 60.0  # wall time for the one call: a person is waiting, not the arm
MAX_SHOWN_MEMBERS = 20  # verdicts of the group shown to the model, in store order
MAX_OTHER_GROUPS = 10
MAX_NAME_CHARS = 128  # a provider's model id or finish reason, as recorded
# The readings a verdict's evidence holds and a rule may use, and the digits shown.
READINGS = tuple(sorted(FIELD_BOUNDS))
READING_DIGITS = 4
# Bump when `proposal_context` changes shape: it is part of `prompt_version`.
CONTEXT_VERSION = 1
# The stall rule the groups come from, as the mock's replay runs it.
STALL_WINDOW_S = MonitorConfig().stall_window_s

Outcome = Literal["candidate", "rejected", "timeout", "cap_reached", "error"]

_ROLE = f"""\
You write Monitor rules for a robotic cable-insertion cell. The Monitor watches each skill and
reports a failure class; when no rule describes a failure it reports UNKNOWN and a planner is
asked what to do. The user message is one group of UNKNOWN verdicts that recur with the same
signature, as JSON: the skill, the rule that fired (`insert_stalled`, after {STALL_WINDOW_S:g} s
without progress), the readings at each verdict, their ranges, and the other groups'
signatures.

Propose one rule that names this group's failure, and the plan for it. A rule is a conjunction
of 1 to {MAX_CONDITIONS} threshold conditions on force_n (N, this sample), torque_nm (Nm, the
magnitude, this sample), xy_error_mm (mm, this sample) and progress_mm (mm the plug went down
over the rule's window, {MIN_WINDOW_S} to {MAX_WINDOW_S} s). It fires once its window is full
and every condition holds, only while its skill runs. It must:
- fire on this group's failures before `insert_stalled` would, so use a window under
  {STALL_WINDOW_S:g} s;
- not fire during a healthy insertion, which makes progress;
- not match the other groups;
- name the group's skill.

The plan is what recovery does when the rule fires: switch_skill (to another insertion skill,
named in `skill`), retry, abort, or escalate (hand the task to a person). If no skill is likely
to succeed, abort or escalate rather than spend the recovery budget.

The name is upper case, 3 to 40 characters, and says what the failure is.
Answer by calling the `{TOOL_NAME}` tool exactly once."""


def candidate_json_schema(insertion_skills: Sequence[str]) -> dict[str, object]:
    """The candidate language as one JSON schema for the model's tool, inlined (no `$ref`,
    which some providers reject). Its keys and choices come from the pydantic models; the
    registered skills and the bounds are what the models check in validators.

    It tells the model the shape; `Candidate.parse` and `check_registry` still decide."""
    condition = {
        "type": "object",
        "properties": {
            "field": {"type": "string", "enum": list(READINGS)},
            "op": {"type": "string", "enum": list(get_args(Op))},
            "value": {"type": "number", "description": "within the field's bounds"},
        },
        "required": list(Condition.model_fields),
        "additionalProperties": False,
    }
    rule = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": NAME_PATTERN},
            "skill": {"type": "string", "enum": sorted(insertion_skills)},
            "window_s": {"type": "number", "minimum": MIN_WINDOW_S, "maximum": MAX_WINDOW_S},
            "conditions": {
                "type": "array",
                "items": condition,
                "minItems": 1,
                "maxItems": MAX_CONDITIONS,
            },
        },
        "required": list(PromotedRule.model_fields),
        "additionalProperties": False,
    }
    plan = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(get_args(PromotedPlan.model_fields["action"].annotation)),
            },
            "skill": {
                "type": ["string", "null"],
                "enum": [*sorted(insertion_skills), None],
                "description": "the skill to switch to; null for the other actions",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"rule": rule, "plan": plan},
        "required": list(Candidate.model_fields),
        "additionalProperties": False,
    }


def proposal_context(
    group: Group, others: Sequence[Group], registry: SkillRegistry
) -> dict[str, Any]:
    """What the model is told: the group's readings and the language's bounds. No run ids and
    no injected-fault labels."""
    shown = [_readings(m.evidence) for m in group.members[:MAX_SHOWN_MEMBERS]]
    every = [_readings(m.evidence) for m in group.members]
    return {
        "group": group.signature.label(),
        "skill": group.signature.skill,
        "rule": group.signature.rule,
        "verdicts": len(group.members),
        "runs": len(group.runs),
        "readings": shown,
        "ranges": _ranges(every),
        "other_groups": [
            {
                "group": g.signature.label(),
                "verdicts": len(g.members),
                "ranges": _ranges([_readings(m.evidence) for m in g.members]),
            }
            for g in others[:MAX_OTHER_GROUPS]
        ],
        "bounds": {f: list(b) for f, b in sorted(FIELD_BOUNDS.items())},
        "insertion_skills": {n: _description(registry, n) for n in registry.insertion_skills()},
    }


@dataclass(frozen=True)
class Proposal:
    """One proposal call, as `cellrt promote propose --out` writes it."""

    group: str
    outcome: Outcome
    errors: tuple[str, ...]  # refusal codes, for `rejected`
    error: str | None  # the exception type, for `error`
    candidate: Mapping[str, Any] | None
    candidate_hash: str | None
    provider: str
    model: str  # as requested
    max_tokens: int  # the call's settings, as sent
    temperature: float
    deadline_s: float
    reply_model: str | None  # as the provider named it, scrubbed and cut
    prompt: str  # the messages sent, as JSON, scrubbed and cut
    response: str | None  # the raw answer, scrubbed and cut
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    latency_s: float | None
    prompt_version: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["errors"] = list(self.errors)
        data["candidate"] = None if self.candidate is None else dict(self.candidate)
        return {"proposal_version": PROPOSAL_VERSION, **data}


def propose(
    client: ChatClient,
    config: LlmPlannerConfig,
    group: Group,
    others: Sequence[Group],
    registry: SkillRegistry,
    *,
    # A model call's latency is wall time by design, as in the planner.
    timer: Callable[[], float] = time.perf_counter,  # noqa: TID251
) -> Proposal:
    """Ask once for a candidate for `group`; the call is cut at the config's deadline. The
    answer comes through the `propose` tool (or as text when the model sends no tool call)."""
    return asyncio.run(_propose(client, config, group, others, registry, timer))


async def _propose(
    client: ChatClient,
    config: LlmPlannerConfig,
    group: Group,
    others: Sequence[Group],
    registry: SkillRegistry,
    timer: Callable[[], float],
) -> Proposal:
    insertion = registry.insertion_skills()
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "A Monitor rule for the group, and the plan for its failures.",
            "parameters": candidate_json_schema(insertion),
        },
    }
    context = proposal_context(group, others, registry)
    messages = (
        {"role": "system", "content": _ROLE},
        {"role": "user", "content": json.dumps(context, sort_keys=True)},
    )
    request = ChatRequest(
        model=config.model,
        messages=messages,
        tool=tool,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    )
    base = {
        "group": group.signature.label(),
        "provider": scrub(str(client.describe().get("provider", "unknown")))[:MAX_NAME_CHARS],
        "model": config.model,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "deadline_s": config.deadline_s,
        "prompt": scrub(json.dumps(messages), max_chars=MAX_STORED_PROMPT_CHARS),
        "prompt_version": prompt_version(insertion),
    }
    # A model call is wall time by design: this is the one bound on it here.
    deadline = asyncio.timeout(config.deadline_s)  # noqa: TID251
    started = timer()
    try:
        async with deadline:
            reply = await client.complete(request)
    except TimeoutError:
        if deadline.expired():
            return _failed(base, "timeout", None, timer() - started)
        # The client's own timeout, not this deadline: an error like any other.
        return _failed(base, "error", "TimeoutError", timer() - started)
    except ModelCallCapReached:
        return _failed(base, "cap_reached", None, None)
    except Exception as exc:  # the type only: the text may hold anything
        return _failed(base, "error", type(exc).__name__, timer() - started)
    latency_s = max(0.0, timer() - started)
    raw = reply.tool_arguments if reply.tool_arguments is not None else reply.text
    text = _as_text(raw)
    candidate, errors = _checked(text, group, registry)
    return Proposal(
        **base,
        outcome="candidate" if candidate is not None else "rejected",
        errors=errors,
        error=None,
        candidate=None if candidate is None else candidate.model_dump(mode="json"),
        candidate_hash=None if candidate is None else candidate.digest(),
        reply_model=_name(reply.model),
        response=None if text is None else scrub(text, max_chars=MAX_STORED_RESPONSE_CHARS),
        prompt_tokens=_tokens(reply, "prompt_tokens"),
        completion_tokens=_tokens(reply, "completion_tokens"),
        finish_reason=_name(reply.finish_reason),
        latency_s=latency_s,
    )


def prompt_version(insertion_skills: Sequence[str]) -> str:
    """Changes whenever the system prompt, the tool's schema or the user message's shape
    changes (its bounds, its digits, or CONTEXT_VERSION, bumped by hand for a new key)."""
    shape = {
        "context": CONTEXT_VERSION,
        "shown": MAX_SHOWN_MEMBERS,
        "others": MAX_OTHER_GROUPS,
        "readings": READINGS,
        "digits": READING_DIGITS,
    }
    text = json.dumps([_ROLE, candidate_json_schema(insertion_skills), shape], sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _checked(
    text: str | None, group: Group, registry: SkillRegistry
) -> tuple[Candidate | None, tuple[str, ...]]:
    if not text:
        return None, ("candidate refused: empty",)
    try:
        candidate = parse_checked(text, registry)
    except CandidateRefused as exc:
        return None, (str(exc),)
    if candidate.rule.skill != group.signature.skill:
        # A rule watches only the skill its group came from (ADR 021, 5A).
        return None, ("candidate refused: rule.skill: not_the_group_skill",)
    return candidate, ()


def _failed(
    base: Mapping[str, Any], outcome: Outcome, error: str | None, latency_s: float | None
) -> Proposal:
    return Proposal(
        **base,
        outcome=outcome,
        errors=(),
        error=error,
        candidate=None,
        candidate_hash=None,
        reply_model=None,
        response=None,
        prompt_tokens=None,
        completion_tokens=None,
        finish_reason=None,
        latency_s=None if latency_s is None else max(0.0, latency_s),
    )


def _as_text(raw: object) -> str | None:
    """The answer as text for the parser: a string as it came, a structure a client already
    parsed as JSON, anything else None (refused as empty)."""
    if raw is None or isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw)
    except (TypeError, ValueError, RecursionError):
        return None


def _readings(evidence: Mapping[str, Any]) -> dict[str, float]:
    """The readings a rule can test, as the rule reads them: torque as a magnitude (the stall
    rule's evidence keeps its sign; a promoted rule and the grouping use `abs`)."""
    out: dict[str, float] = {}
    for key in READINGS:
        value = evidence.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            reading = abs(float(value)) if key == "torque_nm" else float(value)
            out[key] = round(reading, READING_DIGITS)
    return out


def _ranges(readings: Sequence[Mapping[str, float]]) -> dict[str, list[float]]:
    """Per reading: min, median, max."""
    out: dict[str, list[float]] = {}
    for key in READINGS:
        values = [r[key] for r in readings if key in r]
        if values:
            median = round(statistics.median(values), READING_DIGITS)
            out[key] = [min(values), median, max(values)]
    return out


def _description(registry: SkillRegistry, name: str) -> str:
    """What the skill does, in one line, cut as the planner cuts it."""
    return str(getattr(registry.get(name), "description", ""))[:MAX_DESCRIPTION_CHARS]


def _name(value: object) -> str | None:
    return None if not isinstance(value, str) else scrub(value)[:MAX_NAME_CHARS]


def _tokens(reply: ChatReply, field: str) -> int | None:
    value = getattr(reply, field)
    return value if type(value) is int and value >= 0 else None
