"""A model as the task planner (ADR 023), through the `ChatClient` seam with a scripted client:
its answer is parsed, checked like any plan, and traced; nothing it writes reaches the log
unscrubbed, and replay never needs the model. No test calls a real model."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from cellrt.eval.runner import run_eval, run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.planner.llm import ChatReply, ChatRequest, LlmPlannerConfig
from cellrt.planner.llm_task import MAX_SKILL_DESCRIPTION_CHARS, TOOL_NAME, LlmTaskPlanner
from cellrt.planner.nim import ModelCallCapReached
from cellrt.planner.schemas import audit_text
from cellrt.planner.task_plan import (
    MAX_ANSWER_CHARS,
    MAX_PARSE_CODES,
    MAX_PLAN_STEPS,
    TracedPlan,
)
from cellrt.replay import replay_lines
from cellrt.runtime.events import (
    Event,
    RunFinished,
    RunStarted,
    SkillStarted,
    TaskPlanned,
    TaskPlanRejected,
    TaskPlanResponded,
)
from cellrt.runtime.store import EventStore
from cellrt.skills.mock.skills import (
    Align,
    Approach,
    InsertAct,
    InsertScripted,
    Retract,
    Verify,
    WaitSensor,
)
from cellrt.skills.registry import SkillRegistry
from cellrt.trace_export import export_trace

TASKS = Path(__file__).resolve().parent.parent / "eval" / "scenarios" / "phase3-tasks.yaml"
ACT_PLAN = '{"steps": ["approach", "align", "insert_act", "verify"]}'


def _sc() -> Scenario:
    return {s.id: s for s in load_scenarios(TASKS)}["sc"]


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


@dataclass
class NeverAnswers:
    cancelled: bool = False

    def describe(self) -> Mapping[str, str]:
        return {"provider": "silent"}

    async def complete(self, request: ChatRequest) -> ChatReply:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")  # pragma: no cover


def _reply(arguments: str | None = ACT_PLAN, *, text: str | None = None) -> ChatReply:
    return ChatReply(
        model="test-model",
        tool_arguments=arguments,
        text=text,
        prompt_tokens=900,
        completion_tokens=20,
        finish_reason="tool_calls",
    )


def _planner(client: object, **config: object) -> LlmTaskPlanner:
    return LlmTaskPlanner(client, LlmPlannerConfig(model="test-model", **config))  # type: ignore[arg-type]


def _of(events: Sequence[Event], kind: type) -> list:
    return [e.payload for e in events if isinstance(e.payload, kind)]


def _user(request: ChatRequest) -> dict:
    return json.loads(request.messages[1]["content"])


def test_a_valid_model_plan_is_traced_recorded_and_run(store: EventStore) -> None:
    client = ScriptedClient([_reply()])

    done = run_unit(_sc(), "B", 0, store, tree=_planner(client))
    events = store.events(done.run_id)

    assert [e.type for e in events[:3]] == ["run_started", "task_plan_responded", "task_planned"]
    [responded] = _of(events, TaskPlanResponded)
    assert (responded.planner, responded.provider, responded.model) == (
        "llm",
        "scripted",
        "test-model",
    )
    assert (responded.prompt_tokens, responded.completion_tokens) == (900, 20)
    assert responded.response == ACT_PLAN
    [planned] = _of(events, TaskPlanned)
    assert planned.steps == ["approach", "align", "insert_act", "verify"]
    assert [s.skill for s in _of(events, SkillStarted)] == planned.steps
    assert done.outcome == "success" and len(client.requests) == 1


def test_the_model_is_given_the_task_and_the_plannable_skills_with_their_contracts(
    store: EventStore,
) -> None:
    client = ScriptedClient([_reply()])

    run_unit(_sc(), "C", 0, store, tree=_planner(client))

    [request] = client.requests
    assert request.tool is not None and request.tool["function"]["name"] == TOOL_NAME  # type: ignore[index]
    items = request.tool["function"]["parameters"]["properties"]["steps"]  # type: ignore[index]
    assert items["maxItems"] == MAX_PLAN_STEPS
    plannable = ["align", "approach", "insert_act", "insert_scripted", "verify"]
    assert items["items"]["enum"] == plannable  # recovery helpers are not offered
    user = _user(request)
    assert user["task"]["plug_type"] == "sc" and user["goal"] == "verified"
    assert sorted(user["skills"]) == plannable
    assert user["skills"]["align"] == {
        "does": user["skills"]["align"]["does"],
        "needs": "at_port",
        "gives": "aligned",
    }
    assert "twists" in user["skills"]["insert_act"]["does"]
    # The fault is the scenario's, never the task's: nothing about it reaches the model.
    assert "yaw" not in json.dumps(request.messages)


@pytest.mark.parametrize(
    ("answer", "errors"),
    [
        ("not json", ["answer: json_invalid"]),
        ('{"steps": []}', ["steps: too_short"]),
        ('{"steps": [1, "align"]}', ["steps.0: string_type"]),
        ('{"steps": ["approach"], "why": "x"}', ["<key>: extra_forbidden"]),
        ('{"stepz\\u001b[2J": ["approach"]}', ["steps: missing", "<key>: extra_forbidden"]),
        ('{"steps": ["approach", "align", "screw_in", "verify"]}',
         ["step 3: unregistered", "step 4: needs_unmet"]),
        ('{"steps": ["approach", "align", "retract", "insert_act", "verify"]}',
         ["step 3: no_contract"]),
        ('{"steps": ["approach", "insert_act", "verify"]}', ["step 2: needs_unmet"]),
    ],
)  # fmt: skip
def test_an_answer_that_is_not_a_valid_plan_is_rejected_with_codes_before_any_motion(
    store: EventStore, answer: str, errors: list[str]
) -> None:
    done = run_unit(_sc(), "B", 0, store, tree=_planner(ScriptedClient([_reply(answer)])))
    events = store.events(done.run_id)

    assert [e.type for e in events] == [
        "run_started",
        "task_plan_responded",
        "task_plan_rejected",
        "run_finished",
    ]
    [rejected] = _of(events, TaskPlanRejected)
    assert rejected.errors == errors
    assert done.outcome == "aborted"


def test_an_answer_with_many_errors_keeps_only_the_first_codes(store: EventStore) -> None:
    eight_numbers = '{"steps": [0, 1, 2, 3, 4, 5, 6, 7], "a": 1, "b": 2, "c": 3}'  # 11 errors

    done = run_unit(_sc(), "A", 0, store, tree=_planner(ScriptedClient([_reply(eight_numbers)])))

    [rejected] = _of(store.events(done.run_id), TaskPlanRejected)
    assert len(rejected.errors) == MAX_PARSE_CODES
    assert (
        rejected.errors
        == [f"steps.{i}: string_type" for i in range(8)] + ["<key>: extra_forbidden"] * 2
    )


@pytest.mark.parametrize(
    "answer",
    [
        _reply('{"steps": ["approach' + chr(0xD800) + '"]}'),  # text, as a host may decode it
        ChatReply("test-model", {"steps": [chr(0xD800)]}, None, 1, 1, "tool_calls"),  # type: ignore[arg-type]
    ],
)
def test_a_lone_surrogate_in_the_answer_is_stored_replaced_and_the_run_ends(
    store: EventStore, answer: ChatReply
) -> None:
    done = run_unit(_sc(), "B", 0, store, tree=_planner(ScriptedClient([answer])))

    events = store.events(done.run_id)
    assert [e.type for e in events] == [
        "run_started",
        "task_plan_responded",
        "task_plan_rejected",
        "run_finished",
    ]
    # Stored at all means no lone surrogate was left: the store writes UTF-8. (A parsed answer
    # is recorded by its repr, which spells the character as the escape "\ud800", plain text.)
    assert chr(0xD800) not in "".join(e.model_dump_json() for e in events)


def test_a_text_answer_without_the_tool_call_is_parsed_as_the_answer(store: EventStore) -> None:
    client = ScriptedClient([_reply(None, text="First approach, then align.")])

    done = run_unit(_sc(), "A", 0, store, tree=_planner(client))

    [rejected] = _of(store.events(done.run_id), TaskPlanRejected)
    assert rejected.errors == ["answer: json_invalid"]


def test_an_answer_too_large_to_parse_is_refused_unread(store: EventStore) -> None:
    huge = '{"steps": ["' + "a" * MAX_ANSWER_CHARS + '"]}'

    done = run_unit(_sc(), "A", 0, store, tree=_planner(ScriptedClient([_reply(huge)])))

    events = store.events(done.run_id)
    [rejected] = _of(events, TaskPlanRejected)
    assert rejected.errors == ["answer: too_large"]
    [responded] = _of(events, TaskPlanResponded)
    assert len(responded.response) <= 4_000


def test_a_model_that_never_answers_is_cancelled_at_the_deadline_before_any_motion(
    store: EventStore,
) -> None:
    client = NeverAnswers()

    done = run_unit(_sc(), "B", 0, store, tree=_planner(client, deadline_s=0.05))
    events = store.events(done.run_id)

    assert [e.type for e in events] == ["run_started", "task_plan_rejected"]
    [rejected] = _of(events, TaskPlanRejected)
    assert rejected.errors == ["planner_timed_out"] and client.cancelled
    assert done.outcome is None  # no answer: the unit is not finished (#63 review F2)


def test_a_call_past_the_cap_stops_the_run_unfinished_and_says_why(store: EventStore) -> None:
    tree = _planner(ScriptedClient([ModelCallCapReached()]))

    with pytest.raises(ModelCallCapReached):
        run_unit(_sc(), "B", 0, store, tree=tree)

    [run] = store.runs()
    events = store.events(run.run_id)
    assert [e.type for e in events] == ["run_started", "task_plan_rejected"]
    [rejected] = _of(events, TaskPlanRejected)
    assert rejected.errors == ["cap_reached"]


def test_an_eval_stops_at_the_cap_and_keeps_the_units_it_finished(store: EventStore) -> None:
    # Two answers and then the cap: the third unit stops the eval; nothing after it runs.
    client = ScriptedClient([_reply(), _reply(), ModelCallCapReached()])

    with pytest.raises(ModelCallCapReached):
        run_eval([_sc()], ["A"], 4, store, tree=_planner(client))

    finished = [r.finished for r in store.runs()]
    assert finished == [True, True, False]


def test_settings_that_reuse_a_model_plan_keep_no_trace_of_their_own(store: EventStore) -> None:
    client = ScriptedClient([_reply()])  # one answer: a second call would fail the test

    a, b = run_eval([_sc()], ["A", "B"], 1, store, tree=_planner(client)).run_ids

    assert len(client.requests) == 1
    assert len(_of(store.events(a), TaskPlanResponded)) == 1
    assert _of(store.events(b), TaskPlanResponded) == []
    [planned] = _of(store.events(b), TaskPlanned)
    assert planned.reused_from == a


def test_a_resumed_eval_reuses_a_model_plan_recorded_after_its_trace(store: EventStore) -> None:
    # The plan is the third event (after run_started and task_plan_responded): the resume reads
    # that far, so it does not ask again.
    client = ScriptedClient([_reply()])
    tree = _planner(client)

    [a] = run_eval([_sc()], ["A"], 1, store, tree=tree).run_ids
    again = run_eval([_sc()], ["A", "B"], 1, store, tree=tree)

    assert len(client.requests) == 1 and again.unfinished == []
    [planned] = _of(store.events(again.run_ids[1]), TaskPlanned)
    assert planned.reused_from == a


def test_secrets_in_the_answer_or_an_error_never_reach_the_log(store: EventStore) -> None:
    leaky = '{"steps": ["approach"], "note": "api_key=sk-live0123456789abcdef"}'
    first = run_unit(_sc(), "A", 0, store, tree=_planner(ScriptedClient([_reply(leaky)])))
    error = RuntimeError("401 for key nvapi-0123456789abcdefghijklmnopqrstuvwxyzABCDEF")
    second = run_unit(_sc(), "B", 0, store, tree=_planner(ScriptedClient([error])))

    logged = "".join(e.model_dump_json() for r in (first, second) for e in store.events(r.run_id))
    assert "sk-live" not in logged and "nvapi-" not in logged
    [rejected] = _of(store.events(second.run_id), TaskPlanRejected)
    assert rejected.errors == ["planner_error: RuntimeError"]


def test_replay_shows_the_model_call_and_the_plan_from_the_log_alone(store: EventStore) -> None:
    client = ScriptedClient([_reply()])
    done = run_unit(_sc(), "B", 0, store, tree=_planner(client))

    lines = list(replay_lines(store.events(done.run_id)))

    assert "llm scripted/test-model" in lines[1] and "tokens=900+20" in lines[1]
    assert "llm: approach -> align -> insert_act -> verify" in lines[2]
    assert len(client.requests) == 1  # replay asked nothing


def test_the_model_settings_and_prompt_version_count_in_the_tree_part(store: EventStore) -> None:
    runs = [
        run_unit(_sc(), "A", 0, store, tree=_planner(ScriptedClient([_reply()]), **settings))
        for settings in ({}, {}, {"temperature": 0.5})
    ]

    parts = [store.events(r.run_id)[0].payload for r in runs]
    assert all(isinstance(p, RunStarted) for p in parts)
    trees = [p.config_parts["tree"] for p in parts]  # type: ignore[union-attr]
    assert trees[0] == trees[1] != trees[2]
    assert _planner(ScriptedClient([])).describe()["prompt_version"]


def test_the_task_planner_answers_through_its_tool_only() -> None:
    with pytest.raises(ValueError, match="tool"):
        _planner(ScriptedClient([]), mode="json")


def test_answers_a_client_already_parsed_are_checked_as_they_are(store: EventStore) -> None:
    parsed = {"steps": ["approach", "align", "insert_act", "verify"]}
    reply = ChatReply("test-model", parsed, None, 900, 20, "tool_calls")  # type: ignore[arg-type]

    done = run_unit(_sc(), "B", 0, store, tree=_planner(ScriptedClient([reply])))

    events = store.events(done.run_id)
    [planned] = _of(events, TaskPlanned)
    assert planned.steps == parsed["steps"]
    [responded] = _of(events, TaskPlanResponded)
    assert responded.response == audit_text(parsed)  # recorded as bounded text


@dataclass
class _BadTrace:
    name: str = "llm"

    async def plan(self, task: object, registry: object) -> TracedPlan:
        return TracedPlan(raw=ACT_PLAN, trace="api_key=sk-live0123456789abcdef")  # type: ignore[arg-type]

    def describe(self) -> Mapping[str, object]:
        return {}


def test_a_trace_that_is_not_a_trace_is_a_rejection_and_never_echoed(store: EventStore) -> None:
    done = run_unit(_sc(), "B", 0, store, tree=_BadTrace())

    events = store.events(done.run_id)
    assert [e.type for e in events] == ["run_started", "task_plan_rejected", "run_finished"]
    [rejected] = _of(events, TaskPlanRejected)
    assert rejected.errors == ["trace_invalid"]
    assert "sk-live" not in "".join(e.model_dump_json() for e in events)


def test_each_skill_description_the_model_is_told_is_cut(store: EventStore) -> None:
    @dataclass(frozen=True)
    class WordyAct(InsertAct):
        description: ClassVar[str] = "twists " * 100

    wordy = SkillRegistry(
        [Approach(), Align(), InsertScripted(), WordyAct(), Verify(), Retract(), WaitSensor()]
    )
    client = ScriptedClient([_reply()])

    run_unit(_sc(), "B", 0, store, tree=_planner(client), registry=wordy)

    does = _user(client.requests[0])["skills"]["insert_act"]["does"]
    assert len(does) == MAX_SKILL_DESCRIPTION_CHARS < len(WordyAct.description)


def test_the_plan_call_is_an_instant_of_the_exported_trace_without_its_prompt(
    store: EventStore,
) -> None:
    done = run_unit(_sc(), "B", 0, store, tree=_planner(ScriptedClient([_reply()])))

    trace = json.loads(json.dumps(export_trace(store.events(done.run_id)), allow_nan=False))

    [call] = [e for e in trace["traceEvents"] if e["name"].startswith("task_plan_responded")]
    assert call["ph"] == "i" and call["args"]["response"] == ACT_PLAN
    assert "prompt" not in call["args"]  # `cellrt replay` and the log keep it


def test_a_rejected_model_plan_ends_the_run_as_finished(store: EventStore) -> None:
    done = run_unit(_sc(), "C", 0, store, tree=_planner(ScriptedClient([_reply("[]")])))

    finished = store.events(done.run_id)[-1].payload
    assert isinstance(finished, RunFinished)
    assert (finished.outcome, finished.reason, finished.steps) == (
        "aborted",
        "task plan rejected",
        0,
    )
