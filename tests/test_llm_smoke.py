"""The smoke call (#49): one realistic failure, sent once through the model planner, to learn
whether a model's tool calling works before an eval relies on it. The SDK call is faked."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nim_fakes import FAKE_KEY, NIM_MODEL, SWITCH_TO_ACT, FakeCreate
from nim_fakes import completion as _completion

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.planner.base import FailureContext
from cellrt.planner.llm import LlmPlannerConfig
from cellrt.planner.nim import KEY_ENV, ModelCallCap, NimChatClient, nim_client
from cellrt.planner.smoke import SmokeReport, run_smoke, smoke_context
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import mock_registry

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
YAW = {s.id: s for s in load_scenarios(PHASE0)}["yaw_mismatch"]


class _Timer:
    """A wall clock that moves 1.5 s between the two times the planner reads it."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        self._now += 1.5
        return self._now


def _smoke(create: Any, *, mode: str = "tool", deadline_s: float = 30.0) -> SmokeReport:
    client = NimChatClient(create, cap=ModelCallCap(1))
    config = LlmPlannerConfig(model=NIM_MODEL, mode=mode, deadline_s=deadline_s)  # type: ignore[arg-type]
    return run_smoke(client, config, timer=_Timer())


def test_a_tool_call_that_validates_is_reported_with_its_cost() -> None:
    report = _smoke(FakeCreate([_completion()]))

    assert report == SmokeReport(
        max_tokens=1024,
        reasoning="default",
        answer="tool_call",
        valid=True,
        decision="switch_skill",
        errors=(),
        error=None,
        model=NIM_MODEL,
        prompt_tokens=812,
        completion_tokens=37,
        finish_reason="tool_calls",
        latency_s=1.5,
    )


def test_a_text_answer_in_json_mode_is_reported_as_text() -> None:
    create = FakeCreate([_completion(arguments=None, content=SWITCH_TO_ACT, finish_reason="stop")])

    report = _smoke(create, mode="json")

    assert (report.answer, report.valid, report.decision) == ("text", True, "switch_skill")
    assert "tools" not in create.calls[0]


def test_a_text_answer_to_a_tool_request_shows_tool_calling_did_not_work() -> None:
    prose = _completion(arguments=None, content="I would switch to insert_act.", usage=None)

    report = _smoke(FakeCreate([prose]))

    assert (report.answer, report.valid, report.decision) == ("text", False, None)
    assert report.errors == ("<root>: not_json",)
    assert (report.prompt_tokens, report.completion_tokens) == (None, None)


def test_an_answer_that_fails_validation_lists_the_codes_only() -> None:
    wrong = _completion(arguments='{"kind": "switch_skill", "skill": "insert_scripted"}')

    report = _smoke(FakeCreate([wrong]))

    assert (report.answer, report.valid) == ("tool_call", False)
    assert report.errors == ("skill: same_as_failed_skill",)


def test_no_answer_at_all_is_reported_as_none() -> None:
    report = _smoke(FakeCreate([_completion(arguments=None, finish_reason="length")]))

    assert (report.answer, report.valid, report.finish_reason) == ("none", False, "length")


class APIStatusError(Exception):
    """Named like the SDK's error, with text that holds a key."""


def test_a_failed_call_is_reported_by_its_type_only() -> None:
    report = _smoke(FakeCreate([APIStatusError(f"401 for api_key={FAKE_KEY}")]))

    assert (report.answer, report.valid, report.error) == ("error", False, "APIStatusError")
    assert report.latency_s is None and report.model is None
    assert FAKE_KEY not in json.dumps(report.as_dict())


@dataclass
class _Hanging:
    """A `create` that never answers, and notes that it was cancelled."""

    cancelled: list[bool] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> object:
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.append(True)
        raise AssertionError("never reached")


def test_a_call_that_never_answers_is_cut_at_the_planner_deadline() -> None:
    hanging = _Hanging()

    report = _smoke(hanging, deadline_s=0.05)

    assert (report.answer, report.error) == ("error", "TimeoutError")
    assert hanging.cancelled == [True]  # the request was released


def test_the_smoke_call_counts_against_the_cap() -> None:
    create = FakeCreate([_completion(), _completion()])
    client = NimChatClient(create, cap=ModelCallCap(1))
    config = LlmPlannerConfig(model=NIM_MODEL)

    first = run_smoke(client, config)
    second = run_smoke(client, config)

    assert first.valid and (second.answer, second.error) == ("error", "ModelCallCapReached")
    assert len(create.calls) == 1


def test_the_report_names_the_token_budget_and_reasoning_it_asked_with() -> None:
    client = NimChatClient(FakeCreate([_completion(), APIStatusError("down")]), cap=ModelCallCap(2))
    config = LlmPlannerConfig(model=NIM_MODEL, max_tokens=4096, reasoning="off")

    answered = run_smoke(client, config)
    failed = run_smoke(client, config)

    assert (answered.answer, failed.answer) == ("tool_call", "error")
    for report in (answered, failed):
        data = report.as_dict()
        assert (data["max_tokens"], data["reasoning"]) == (4096, "off")


def test_provider_names_in_the_report_are_scrubbed_and_cut() -> None:
    odd = _completion(model=f"evil\x1b[2J{FAKE_KEY}" + "m" * 500, finish_reason="z" * 500)

    report = _smoke(FakeCreate([odd]))

    assert report.model is not None and report.finish_reason is not None
    assert FAKE_KEY not in report.model and "\x1b" not in report.model
    assert len(report.model) <= 128 and len(report.finish_reason) == 128


def test_the_report_is_plain_data_without_the_key() -> None:
    def connect(*, base_url: str, api_key: str, timeout_s: float) -> FakeCreate:
        return FakeCreate([_completion()])

    client = nim_client(cap=ModelCallCap(1), env={KEY_ENV: FAKE_KEY}, connect=connect)

    report = run_smoke(client, LlmPlannerConfig(model=NIM_MODEL))

    data = report.as_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["answer"] == "tool_call" and data["errors"] == []
    assert FAKE_KEY not in json.dumps(data) + repr(report)


def test_the_smoke_sends_the_yaw_mismatch_unknown_as_the_mock_produces_it(
    store: EventStore,
) -> None:
    @dataclass
    class Recording:
        asked: list[FailureContext] = field(default_factory=list)

        async def triage(self, ctx: FailureContext) -> object:
            self.asked.append(ctx)
            return {"kind": "abort", "reason": "recording only"}

    recording = Recording()
    run_unit(YAW, "C", 0, store, planner=recording)
    create = FakeCreate([_completion()])

    _smoke(create)

    [from_the_mock] = recording.asked
    assert smoke_context(mock_registry()) == from_the_mock
    sent = json.loads(create.calls[0]["messages"][1]["content"])
    assert sent == from_the_mock.as_dict()
    assert (sent["skill"], sent["failure_class"], sent["rule"]) == (
        "insert_scripted",
        "UNKNOWN",
        "insert_stalled",
    )
