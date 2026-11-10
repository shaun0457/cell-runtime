"""Setting C with a model planner (#45): a chat client answers, the existing validation decides
what runs, and every call leaves its trace in the event log. Tests never call a real model: the
client here is scripted."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.behaviours import MAX_DESCRIPTION_CHARS, MAX_TRACE_NAME_CHARS
from cellrt.planner.base import (
    MAX_STORED_PROMPT_CHARS,
    MAX_STORED_RESPONSE_CHARS,
    FailureContext,
    PlannerTrace,
    Traced,
)
from cellrt.planner.llm import (
    MAX_MODEL_CHARS,
    MAX_TEMPERATURE,
    MAX_TOKENS_LIMIT,
    SYSTEM_PROMPTS,
    ChatReply,
    ChatRequest,
    LlmPlanner,
    LlmPlannerConfig,
)
from cellrt.planner.rule_planner import RulePlanner
from cellrt.replay import replay_lines
from cellrt.runtime.events import Event, PlannerResponded, RunStarted
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import (
    Align,
    Approach,
    InsertAct,
    InsertScripted,
    Retract,
    Verify,
    WaitSensor,
    mock_registry,
)
from cellrt.skills.registry import SkillRegistry

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE0)}
YAW = SCENARIOS["yaw_mismatch"]
SWITCH_TO_ACT = '{"kind": "switch_skill", "skill": "insert_act"}'


@dataclass
class ScriptedClient:
    """Answers with scripted replies in order and records every request it was sent."""

    replies: list[ChatReply | BaseException]
    requests: list[ChatRequest] = field(default_factory=list)

    def describe(self) -> Mapping[str, str]:
        return {"provider": "scripted"}

    async def complete(self, request: ChatRequest) -> ChatReply:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _tool_reply(arguments: str, *, model: str = "test-model") -> ChatReply:
    return ChatReply(
        model=model,
        tool_arguments=arguments,
        text=None,
        prompt_tokens=812,
        completion_tokens=37,
        finish_reason="tool_calls",
    )


class _Timer:
    """A wall clock that moves 0.25 s between the two times the planner reads it."""

    def __init__(self) -> None:
        self._now = 100.0

    def __call__(self) -> float:
        self._now += 0.25
        return self._now


def _planner(client: ScriptedClient, **config: object) -> LlmPlanner:
    settings = {"model": "test-model", **config}
    return LlmPlanner(client, LlmPlannerConfig(**settings), timer=_Timer())  # type: ignore[arg-type]


def _run(store: EventStore, scenario: Scenario, seed: int = 0, **kwargs: object) -> list[Event]:
    run = run_unit(scenario, "C", seed=seed, store=store, **kwargs)  # type: ignore[arg-type]
    return store.events(run.run_id)


def _types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def _responded(events: list[Event]) -> list[PlannerResponded]:
    return [e.payload for e in events if isinstance(e.payload, PlannerResponded)]


def test_a_model_planner_switches_to_insert_act_and_the_yaw_mismatch_task_succeeds(
    store: EventStore,
) -> None:
    client = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])

    events = _run(store, YAW, planner=_planner(client))
    with_rules = _run(store, YAW, planner=RulePlanner())

    assert events[-1].payload.outcome == with_rules[-1].payload.outcome == "success"
    assert "planner_responded" not in _types(with_rules)  # a rule planner's log keeps its shape
    types = _types(events)
    planner_events = [t for t in types if t.startswith("planner_")]
    assert planner_events == ["planner_called", "planner_responded", "planner_decided"]
    recoveries = [e.payload.action for e in events if e.type == "recovery_started"]
    assert recoveries == ["switch_skill:insert_act"]


def test_every_model_call_leaves_its_trace_in_the_log(store: EventStore) -> None:
    client = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])

    [trace] = _responded(_run(store, YAW, planner=_planner(client)))

    assert trace.skill == "insert_scripted"
    assert (trace.provider, trace.model) == ("scripted", "test-model")
    assert trace.response == SWITCH_TO_ACT
    assert (trace.prompt_tokens, trace.completion_tokens) == (812, 37)
    assert trace.finish_reason == "tool_calls"
    assert trace.latency_s == 0.25
    sent = json.loads(trace.prompt)
    assert [m["role"] for m in sent] == ["system", "user"]
    assert json.loads(sent[1]["content"])["rule"] == "insert_stalled"


def test_the_model_is_asked_through_the_decide_tool_with_the_failure_context(
    store: EventStore,
) -> None:
    client = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])

    _run(store, YAW, planner=_planner(client, max_tokens=256, temperature=0.2))

    [request] = client.requests
    assert (request.model, request.max_tokens, request.temperature) == ("test-model", 256, 0.2)
    assert request.tool is not None and request.tool["function"]["name"] == "decide"
    kinds = {
        v["properties"]["kind"]["const"] for v in request.tool["function"]["parameters"]["oneOf"]
    }
    assert kinds == {"switch_skill", "retry", "abort", "escalate"}
    assert '"escalate"' in SYSTEM_PROMPTS["json"]  # the JSON answer's schema has it too
    context = json.loads(request.messages[1]["content"])
    assert (context["skill"], context["failure_class"]) == ("insert_scripted", "UNKNOWN")
    assert context["options"] == ["insert_act"]  # never the failed skill
    assert "twist" in context["descriptions"]["insert_act"]


def test_a_json_text_answer_goes_through_the_same_validation(store: EventStore) -> None:
    reply = ChatReply(
        model="test-model",
        tool_arguments=None,
        text=SWITCH_TO_ACT,
        prompt_tokens=None,
        completion_tokens=None,
        finish_reason="stop",
    )
    client = ScriptedClient([reply])

    events = _run(store, YAW, planner=_planner(client, mode="json"))

    [request] = client.requests
    assert request.tool is None
    assert "switch_skill" in request.messages[0]["content"]  # the schema is in the prompt
    assert events[-1].payload.outcome == "success"
    [trace] = _responded(events)
    assert (trace.prompt_tokens, trace.completion_tokens) == (None, None)


def test_malformed_model_output_is_rejected_and_nothing_it_asked_for_runs(
    store: EventStore,
) -> None:
    for answer in ('{"kind": "switch_skill", "skill": "rm -rf /"}', "", "I would retry."):
        client = ScriptedClient([_tool_reply(answer)])

        events = _run(store, YAW, planner=_planner(client))

        assert "planner_output_rejected" in _types(events)
        assert "recovery_started" not in _types(events)
        assert events[-1].payload.outcome == "aborted"


def test_an_answer_with_neither_tool_call_nor_text_is_rejected(store: EventStore) -> None:
    empty = ChatReply(
        model="test-model",
        tool_arguments=None,
        text=None,
        prompt_tokens=10,
        completion_tokens=0,
        finish_reason="length",
    )

    events = _run(store, YAW, planner=_planner(ScriptedClient([empty])))

    [rejected] = [e.payload for e in events if e.type == "planner_output_rejected"]
    assert rejected.errors == ["<root>: not_json"]
    assert _responded(events)[0].finish_reason == "length"


def test_a_client_error_is_recorded_by_its_type_only(store: EventStore) -> None:
    leak = "upstream said: api_key=nvapi-0123456789abcdef0123456789abcdef"
    client = ScriptedClient([ConnectionError(leak)])

    events = _run(store, YAW, planner=_planner(client))

    [rejected] = [e.payload for e in events if e.type == "planner_output_rejected"]
    assert rejected.errors == ["<root>: planner_raised ConnectionError"]
    assert events[-1].payload.outcome == "aborted"
    assert all("nvapi" not in e.model_dump_json() for e in events)


def test_the_stored_response_is_scrubbed_and_cut(store: EventStore) -> None:
    long_answer = "x" * (MAX_STORED_RESPONSE_CHARS + 1_000)
    with_secret = "I think api_key=nvapi-0123456789abcdef0123456789abcdef helps"
    client = ScriptedClient([_tool_reply(long_answer)])
    leaky = ScriptedClient([_tool_reply(with_secret)])

    [cut] = _responded(_run(store, YAW, planner=_planner(client)))
    [scrubbed] = _responded(_run(store, YAW, planner=_planner(leaky)))

    assert len(cut.response) == MAX_STORED_RESPONSE_CHARS
    assert "nvapi" not in scrubbed.response and "<redacted>" in scrubbed.response


@dataclass(frozen=True)
class _WordyInsertAct(InsertAct):
    description: ClassVar[str] = "Twists the plug into line. " * 1_000


@dataclass(frozen=True)
class _WordyRetract(Retract):
    description: ClassVar[str] = "Lifts the plug back up. " * 100


def test_each_skill_description_is_cut(store: EventStore) -> None:
    wordy = SkillRegistry(
        [
            Approach(),
            Align(),
            InsertScripted(),
            _WordyInsertAct(),
            Verify(),
            Retract(),
            WaitSensor(),
        ]
    )
    planner = _Recording()

    events = _run(store, YAW, planner=planner, registry=wordy)

    [ctx] = planner.asked
    assert len(ctx.descriptions["insert_act"]) == MAX_DESCRIPTION_CHARS
    [called] = [e.payload for e in events if e.type == "planner_called"]
    assert len(called.context["descriptions"]["insert_act"]) == MAX_DESCRIPTION_CHARS


def test_a_lone_surrogate_in_the_answer_is_stored_replaced_and_the_run_ends(
    store: EventStore,
) -> None:
    client = ScriptedClient([_tool_reply('{"kind": "retry"}' + chr(0xD800))])

    events = _run(store, YAW, planner=_planner(client))

    [trace] = _responded(events)
    assert trace.response == '{"kind": "retry"}?'
    assert events[-1].type == "run_finished"


def test_the_stored_prompt_is_cut(store: EventStore) -> None:
    helpers = [_WordyRetract(name=f"helper_{i:02d}") for i in range(60)]
    standard = [Approach(), Align(), InsertScripted(), InsertAct(), Verify(), Retract()]
    wordy = SkillRegistry([*standard, WaitSensor(), *helpers])
    client = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])

    [trace] = _responded(_run(store, YAW, planner=_planner(client), registry=wordy))

    assert (
        len(client.requests[0].messages[0]["content"])
        + len(client.requests[0].messages[1]["content"])
        > MAX_STORED_PROMPT_CHARS
    )
    assert len(trace.prompt) == MAX_STORED_PROMPT_CHARS


@dataclass
class _Recording:
    asked: list[FailureContext] = field(default_factory=list)

    async def triage(self, ctx: FailureContext) -> object:
        self.asked.append(ctx)
        return {"kind": "switch_skill", "skill": "insert_act"}


def test_every_planner_is_told_the_step_options_and_what_each_skill_does(
    store: EventStore,
) -> None:
    planner = _Recording()

    _run(store, YAW, planner=planner)

    [ctx] = planner.asked
    assert ctx.options == ("insert_act",)
    registry = mock_registry()
    assert set(ctx.descriptions) == set(registry.names())
    assert all(ctx.descriptions[name] for name in registry.names())


def _started(events: list[Event]) -> RunStarted:
    payload = events[0].payload
    assert isinstance(payload, RunStarted)
    return payload


def test_the_model_configuration_counts_in_the_config_hash(store: EventStore) -> None:
    def run_with(**config: object) -> RunStarted:
        client = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])
        return _started(_run(store, YAW, planner=_planner(client, **config)))

    first = run_with(model="model-a")
    same = run_with(model="model-a")
    other_model = run_with(model="model-b")
    other_tokens = run_with(model="model-a", max_tokens=99)
    no_reasoning = run_with(model="model-a", reasoning="off")

    assert first.config_hash == same.config_hash
    runs = (first, other_model, other_tokens, no_reasoning)
    assert len({run.config_hash for run in runs}) == 4
    assert other_model.config_parts["planner"] != first.config_parts["planner"]
    assert other_model.config_parts["world"] == first.config_parts["world"]


def test_replay_shows_the_trace_and_never_calls_a_model(tmp_path: Path) -> None:
    db = tmp_path / "llm.sqlite"
    client = ScriptedClient([_tool_reply(SWITCH_TO_ACT, model="nvidia/some-model")])
    with EventStore(db) as store:
        run_id = run_unit(YAW, "C", 0, store, planner=_planner(client)).run_id

    with EventStore(db) as store:  # a fresh store: no planner, no client
        lines = list(replay_lines(store.events(run_id)))

    [line] = [ln for ln in lines if "planner_responded" in ln]
    assert "scripted/nvidia/some-model" in line
    assert "0.25s" in line and "tokens=812+37" in line
    assert len(client.requests) == 1  # replay asked nothing


def test_the_reasoning_setting_goes_with_every_request(store: EventStore) -> None:
    default = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])
    off = ScriptedClient([_tool_reply(SWITCH_TO_ACT)])

    _run(store, YAW, planner=_planner(default))
    _run(store, YAW, planner=_planner(off, reasoning="off"))

    assert [r.reasoning for r in default.requests] == ["default"]
    assert [r.reasoning for r in off.requests] == ["off"]


def test_the_planner_config_is_bounded() -> None:
    LlmPlannerConfig(model="m" * MAX_MODEL_CHARS, max_tokens=MAX_TOKENS_LIMIT, temperature=2.0)
    LlmPlannerConfig(model="m", max_tokens=1, reasoning="off")
    for bad in (
        {"model": ""},
        {"model": "m" * (MAX_MODEL_CHARS + 1)},
        {"model": "m", "max_tokens": 0},
        {"model": "m", "max_tokens": MAX_TOKENS_LIMIT + 1},
        {"model": "m", "temperature": -0.1},
        {"model": "m", "temperature": MAX_TEMPERATURE + 0.1},
        {"model": "m", "mode": "yaml"},
        {"model": "m", "reasoning": "on"},
        {"model": "m", "reasoning": "Off"},
    ):
        with pytest.raises(ValueError):
            LlmPlannerConfig(**bad)  # type: ignore[arg-type]


def _reply(**changes: object) -> ChatReply:
    fields = {
        "model": "test-model",
        "tool_arguments": SWITCH_TO_ACT,
        "text": None,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "finish_reason": "tool_calls",
    }
    return ChatReply(**{**fields, **changes})  # type: ignore[arg-type]


def test_a_reply_with_wrong_types_is_an_event_never_a_crash(store: EventStore) -> None:
    for bad in (_reply(prompt_tokens=1.5), _reply(prompt_tokens=True), _reply(model=42)):
        events = _run(store, YAW, planner=_planner(ScriptedClient([bad])))

        assert events[-1].type == "run_finished"
        [rejected] = [e.payload for e in events if e.type == "planner_output_rejected"]
        assert rejected.errors == ["<root>: planner_raised TypeError"]
        assert events[-1].payload.outcome == "aborted"


def test_tool_arguments_already_parsed_are_validated_and_traced_as_text(
    store: EventStore,
) -> None:
    parsed = _reply(tool_arguments={"kind": "switch_skill", "skill": "insert_act"})

    events = _run(store, YAW, planner=_planner(ScriptedClient([parsed])))

    assert events[-1].payload.outcome == "success"
    [trace] = _responded(events)
    assert "switch_skill" in trace.response


@dataclass
class _BadTrace:
    trace: object

    async def triage(self, ctx: FailureContext) -> object:
        return Traced(raw={"kind": "retry"}, trace=self.trace)  # type: ignore[arg-type]


def test_a_planner_that_returns_a_broken_trace_is_rejected(store: EventStore) -> None:
    for trace in (None, "not a trace"):
        events = _run(store, YAW, planner=_BadTrace(trace))

        assert events[-1].type == "run_finished"
        [rejected] = [e.payload for e in events if e.type == "planner_output_rejected"]
        assert rejected.errors == ["<root>: trace_invalid"]
        assert "planner_responded" not in _types(events)
        assert "recovery_started" not in _types(events)


def test_long_names_in_a_trace_are_cut(store: EventStore) -> None:
    long = _reply(model="m" * 500, finish_reason="z" * 500)  # not hex: the scrubber keeps it

    [trace] = _responded(_run(store, YAW, planner=_planner(ScriptedClient([long]))))

    assert len(trace.model) == len(trace.finish_reason or "") == MAX_TRACE_NAME_CHARS


def test_a_trace_refuses_impossible_values() -> None:
    good = {
        "provider": "p",
        "model": "m",
        "prompt": "[]",
        "response": "{}",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "finish_reason": None,
        "latency_s": 0.0,
    }
    PlannerTrace(**good)  # type: ignore[arg-type]
    for bad in (
        {"latency_s": float("nan")},
        {"latency_s": float("inf")},
        {"latency_s": -0.001},
        {"prompt_tokens": -1},
        {"completion_tokens": -1},
        {"prompt_tokens": False},
        {"model": None},
        {"response": {"kind": "retry"}},
        {"finish_reason": 3},
    ):
        with pytest.raises((TypeError, ValueError)):
            PlannerTrace(**{**good, **bad})  # type: ignore[arg-type]
