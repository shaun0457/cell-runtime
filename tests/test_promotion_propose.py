"""The rule proposer (ADR 021, ticket T4): one model call per UNKNOWN group, through the
planner's `ChatClient` seam. A scripted client answers; no test calls a model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from nim_fakes import FAKE_KEY, FakeCreate, completion

import cellrt.promotion.propose as propose_module
from cellrt.eval.runner import run_eval
from cellrt.eval.scenarios import load_scenarios
from cellrt.monitor.classifier import MonitorConfig
from cellrt.monitor.promoted import (
    FIELD_BOUNDS,
    MAX_CONDITIONS,
    Candidate,
    Condition,
    PromotedPlan,
    PromotedRule,
)
from cellrt.planner.base import MAX_STORED_RESPONSE_CHARS
from cellrt.planner.llm import ChatReply, ChatRequest, LlmPlannerConfig
from cellrt.planner.nim import KEY_ENV, ModelCallCap, nim_client
from cellrt.promotion.groups import Group, Member, Signature, group_unknowns, survey
from cellrt.promotion.propose import (
    MAX_NAME_CHARS,
    MAX_OTHER_GROUPS,
    MAX_SHOWN_MEMBERS,
    TOOL_NAME,
    Proposal,
    candidate_json_schema,
    prompt_version,
    proposal_context,
    propose,
)
from cellrt.promotion.replay import ReplayConfig
from cellrt.promotion.validate import validate
from cellrt.runtime.events import Event
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import mock_registry

REPO = Path(__file__).resolve().parents[1]
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"
ROTATED = "insert_scripted/insert_stalled force <10 N torque >=0.1 Nm"
CANDIDATE: dict[str, Any] = {
    "rule": {
        "name": "PLUG_ROTATED",
        "skill": "insert_scripted",
        "window_s": 0.5,
        "conditions": [
            {"field": "torque_nm", "op": ">=", "value": 0.3},
            {"field": "progress_mm", "op": "<", "value": 0.2},
        ],
    },
    "plan": {"action": "switch_skill", "skill": "insert_act"},
}
ESCALATE = {"action": "escalate", "skill": None}
CONFIG = LlmPlannerConfig(model="fake-model", max_tokens=2048, deadline_s=5.0)


NEVER = None  # a scripted call that never answers


class Scripted:
    """A `ChatClient` that answers from a script, in order, and keeps every request. NEVER is
    a call that waits until it is cancelled, and notes that it was."""

    def __init__(self, *answers: ChatReply | BaseException | None) -> None:
        self._answers = list(answers)
        self.requests: list[ChatRequest] = []
        self.cancelled = False

    def describe(self) -> dict[str, str]:
        return {"provider": "fake"}

    async def complete(self, request: ChatRequest) -> ChatReply:
        self.requests.append(request)
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer is NEVER:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("the deadline should have cut this call")
        return answer


def _reply(arguments: str | None = json.dumps(CANDIDATE), text: str | None = None) -> ChatReply:
    return ChatReply(
        model="fake-model",
        tool_arguments=arguments,
        text=text,
        prompt_tokens=900,
        completion_tokens=120,
        finish_reason="tool_calls",
    )


def _with(**rule: Any) -> str:
    return json.dumps({**CANDIDATE, "rule": {**CANDIDATE["rule"], **rule}})


@pytest.fixture(scope="module")
def phase3_runs(tmp_path_factory: pytest.TempPathFactory) -> list[list[Event]]:
    path = tmp_path_factory.mktemp("propose") / "phase3.sqlite"
    with EventStore(path) as store:
        run_eval(load_scenarios(PHASE3), ["A", "B", "C"], 3, store)
        return [store.events(r.run_id) for r in store.runs()]


@pytest.fixture(scope="module")
def groups(phase3_runs: list[list[Event]]) -> list[Group]:
    return group_unknowns(survey(phase3_runs, ReplayConfig.mock()))


def _ask(groups: list[Group], client: Scripted, config: LlmPlannerConfig = CONFIG) -> Proposal:
    [target] = [g for g in groups if g.signature.label() == ROTATED]
    others = [g for g in groups if g is not target]
    return propose(client, config, target, others, mock_registry())


def test_a_valid_answer_is_a_candidate_the_replay_gates_pass(
    phase3_runs: list[list[Event]], groups: list[Group]
) -> None:
    proposal = _ask(groups, Scripted(_reply()))
    assert (proposal.outcome, proposal.errors, proposal.error) == ("candidate", (), None)
    assert proposal.candidate is not None
    candidate = Candidate.parse(json.dumps(proposal.candidate))
    assert candidate.digest() == proposal.candidate_hash
    assert validate(phase3_runs, candidate, ROTATED, ReplayConfig.mock()).passed
    assert (proposal.prompt_tokens, proposal.completion_tokens) == (900, 120)
    assert proposal.response == json.dumps(CANDIDATE)
    assert (proposal.provider, proposal.model, proposal.reply_model) == (
        "fake",
        "fake-model",
        "fake-model",
    )
    # The call's settings go into the record, so a cut answer can be read against its budget.
    assert (proposal.max_tokens, proposal.temperature, proposal.deadline_s) == (2048, 0.0, 5.0)


def test_the_request_holds_the_group_the_language_and_the_skills(groups: list[Group]) -> None:
    client = Scripted(_reply())
    _ask(groups, client)
    [request] = client.requests
    assert request.tool is not None and request.tool["function"]["name"] == TOOL_NAME  # type: ignore[index]
    assert (request.model, request.max_tokens) == ("fake-model", 2048)
    context = json.loads(request.messages[1]["content"])
    [target] = [g for g in groups if g.signature.label() == ROTATED]
    assert context["group"] == ROTATED
    assert context["verdicts"] == len(target.members) == len(context["readings"])
    assert set(context["readings"][0]) == {"force_n", "torque_nm", "progress_mm", "xy_error_mm"}
    assert set(context["insertion_skills"]) == {"insert_scripted", "insert_act"}
    assert {g["group"] for g in context["other_groups"]} == {
        g.signature.label() for g in groups if g is not target
    }
    # The stall rule is described as the replay runs it.
    stall = MonitorConfig().stall_window_s
    assert f"after {stall:g} s" in request.messages[0]["content"]


def test_the_model_never_sees_run_ids_or_fault_labels(groups: list[Group]) -> None:
    client = Scripted(_reply())
    proposal = _ask(groups, client)
    sent = json.dumps([dict(m) for m in client.requests[0].messages])
    for group in groups:
        for kind in group.injected:  # referee labels (ADR 015)
            assert kind not in sent
        for member in group.members:
            assert member.run_id not in sent
    for scenario in ("baseline", "yaw_mismatch", "damaged_port"):
        assert scenario not in sent
    assert proposal.prompt == json.dumps([dict(m) for m in client.requests[0].messages])


@pytest.mark.parametrize(
    "answer, code",
    [
        ("not json", "json_invalid"),
        (_with(note="x"), "rule.<key>: extra_forbidden"),
        (_with(skill="insert_fancy"), "rule.skill: not_registered"),
        (_with(conditions=[{"field": "torque_nm", "op": ">=", "value": 50.0}]), "out_of_bounds"),
        (_with(window_s=5.0), "rule.window_s: less_than_equal"),
        (
            json.dumps({**CANDIDATE, "plan": {"action": "switch_skill", "skill": "insert_fancy"}}),
            "plan.skill: not_registered",
        ),
        # Registered, but not the group's skill (ADR 021, 5A).
        (
            json.dumps({"rule": {**CANDIDATE["rule"], "skill": "insert_act"}, "plan": ESCALATE}),
            "rule.skill: not_the_group_skill",
        ),
        (None, "empty"),
    ],
)
def test_an_answer_outside_the_language_is_a_recorded_rejection(
    groups: list[Group], answer: str | None, code: str
) -> None:
    proposal = _ask(groups, Scripted(_reply(answer)))
    assert proposal.outcome == "rejected"
    assert proposal.candidate is None and proposal.candidate_hash is None
    [error] = proposal.errors
    assert error.startswith("candidate refused: ") and code in error
    assert proposal.response == answer


def test_a_secret_in_a_rejected_answer_is_kept_out_of_the_codes_and_scrubbed(
    groups: list[Group],
) -> None:
    proposal = _ask(groups, Scripted(_reply(_with(name=FAKE_KEY))))
    assert proposal.outcome == "rejected"
    assert FAKE_KEY not in json.dumps(proposal.as_dict())


def test_an_answer_in_the_text_is_read_when_there_is_no_tool_call(groups: list[Group]) -> None:
    proposal = _ask(groups, Scripted(_reply(None, text=json.dumps(CANDIDATE))))
    assert proposal.outcome == "candidate"


def test_a_stored_answer_is_cut(groups: list[Group]) -> None:
    proposal = _ask(groups, Scripted(_reply("x" * 50_000)))
    assert proposal.outcome == "rejected"
    assert proposal.response is not None and len(proposal.response) == MAX_STORED_RESPONSE_CHARS


def test_the_deadline_cuts_a_call_that_does_not_answer(groups: list[Group]) -> None:
    config = LlmPlannerConfig(model="fake-model", deadline_s=0.05)
    client = Scripted(NEVER)
    proposal = _ask(groups, client, config)
    assert (proposal.outcome, proposal.candidate, proposal.response) == ("timeout", None, None)
    assert proposal.latency_s is not None and proposal.latency_s < 5.0
    assert client.cancelled  # the call is not left running


def test_a_timeout_the_client_raises_itself_is_an_error_not_the_deadline(
    groups: list[Group],
) -> None:
    proposal = _ask(groups, Scripted(TimeoutError("the SDK's own")))
    assert (proposal.outcome, proposal.error) == ("error", "TimeoutError")


def test_the_stored_prompt_and_the_provider_names_are_cut(
    groups: list[Group], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(propose_module, "MAX_STORED_PROMPT_CHARS", 300)
    reply = ChatReply(
        model="m" * 500,
        tool_arguments=json.dumps(CANDIDATE),
        text=None,
        prompt_tokens=1,
        completion_tokens=1,
        finish_reason="z" * 500,  # not hex: scrub would redact a long hex run
    )
    proposal = _ask(groups, Scripted(reply))
    assert len(proposal.prompt) == 300
    assert proposal.reply_model == "m" * MAX_NAME_CHARS
    assert proposal.finish_reason == "z" * MAX_NAME_CHARS


def test_a_failed_call_is_recorded_by_its_type_only(groups: list[Group]) -> None:
    proposal = _ask(groups, Scripted(RuntimeError(f"upstream said {FAKE_KEY}")))
    assert (proposal.outcome, proposal.error) == ("error", "RuntimeError")
    assert FAKE_KEY not in json.dumps(proposal.as_dict())


def _connect(create: FakeCreate) -> Any:
    def connect(*, base_url: str, api_key: str, timeout_s: float) -> FakeCreate:
        return create

    return connect


def _nim(create: FakeCreate, cap: ModelCallCap) -> Any:
    return nim_client(cap=cap, env={KEY_ENV: FAKE_KEY}, connect=_connect(create))


def test_through_nim_the_cap_stops_the_call_after_its_last_one(groups: list[Group]) -> None:
    answer = completion(arguments=json.dumps(CANDIDATE), name=TOOL_NAME)
    create = FakeCreate([answer])
    client = _nim(create, ModelCallCap(1))
    [target] = [g for g in groups if g.signature.label() == ROTATED]
    first = propose(client, CONFIG, target, [], mock_registry())
    second = propose(client, CONFIG, target, [], mock_registry())
    assert (first.outcome, second.outcome) == ("candidate", "cap_reached")
    assert len(create.calls) == 1  # the second request was never sent
    assert create.calls[0]["tools"][0]["function"]["name"] == TOOL_NAME
    for proposal in (first, second):
        assert FAKE_KEY not in json.dumps(proposal.as_dict())


def test_the_tool_schema_is_the_rule_language_inlined() -> None:
    schema = candidate_json_schema(["insert_act", "insert_scripted"])
    assert "$ref" not in json.dumps(schema)
    rule = schema["properties"]["rule"]  # type: ignore[index]
    condition = rule["properties"]["conditions"]
    assert condition["maxItems"] == MAX_CONDITIONS
    assert condition["items"]["properties"]["field"]["enum"] == sorted(FIELD_BOUNDS)
    assert condition["items"]["properties"]["op"]["enum"] == ["<", "<=", ">", ">="]
    assert rule["properties"]["skill"]["enum"] == ["insert_act", "insert_scripted"]
    plan = schema["properties"]["plan"]  # type: ignore[index]
    assert plan["properties"]["action"]["enum"] == ["switch_skill", "retry", "abort", "escalate"]
    assert plan["properties"]["skill"]["enum"] == ["insert_act", "insert_scripted", None]
    # The keys are the models' own: a field added to the language shows up here.
    assert set(schema["properties"]) == set(Candidate.model_fields)  # type: ignore[arg-type]
    assert set(rule["properties"]) == set(rule["required"]) == set(PromotedRule.model_fields)
    assert set(condition["items"]["properties"]) == set(Condition.model_fields)
    assert set(plan["properties"]) == set(PromotedPlan.model_fields)


def _member(i: int, torque: float) -> Member:
    evidence = {"force_n": 5.0, "torque_nm": torque, "progress_mm": 0.1, "xy_error_mm": 0.2}
    return Member(
        run_id=f"x/A/{i}/a1", seq=i, skill="insert_scripted", attempt=1, evidence=evidence
    )


def test_the_readings_shown_are_bounded_and_the_ranges_cover_every_verdict() -> None:
    signature = Signature("insert_scripted", "insert_stalled", "<10", ">=0.1")
    members = tuple(_member(i, 0.4 + i / 100) for i in range(MAX_SHOWN_MEMBERS + 5))
    group = Group(signature, members, {})
    others = [
        Group(Signature("insert_act", "insert_stalled", "<10", f"<{i}"), members[:1], {})
        for i in range(MAX_OTHER_GROUPS + 2)
    ]
    context = proposal_context(group, others, mock_registry())
    assert len(context["readings"]) == MAX_SHOWN_MEMBERS
    assert context["verdicts"] == MAX_SHOWN_MEMBERS + 5
    assert context["ranges"]["torque_nm"][2] == round(0.4 + (MAX_SHOWN_MEMBERS + 4) / 100, 4)
    assert len(context["other_groups"]) == MAX_OTHER_GROUPS


def test_torque_is_shown_as_the_magnitude_a_rule_reads() -> None:
    # The stall rule's evidence keeps the sign (a twist past the port reads negative); a
    # promoted rule tests abs(torque), and the prompt says so.
    signature = Signature("insert_scripted", "insert_stalled", "<10", ">=0.1")
    group = Group(signature, (_member(0, -0.6), _member(1, 0.5)), {})
    context = proposal_context(group, [], mock_registry())
    assert [r["torque_nm"] for r in context["readings"]] == [0.6, 0.5]
    assert context["ranges"]["torque_nm"] == [0.5, 0.55, 0.6]


def test_the_prompt_version_changes_with_the_user_message_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = ["insert_act", "insert_scripted"]
    before = prompt_version(skills)
    monkeypatch.setattr(propose_module, "MAX_SHOWN_MEMBERS", MAX_SHOWN_MEMBERS + 1)
    assert prompt_version(skills) != before
