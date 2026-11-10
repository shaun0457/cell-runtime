"""The NIM chat client (#49): the request it sends, the reply it maps, the key, the base URL and
the call cap. The SDK's `create` call is injected, so no test needs `openai` or the network, and
no test ever sees a real key."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from nim_fakes import FAKE_KEY, NIM_MODEL, SWITCH_TO_ACT, FakeCreate
from nim_fakes import completion as _completion

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.planner.base import MAX_PLANNER_DEADLINE_S
from cellrt.planner.factory import (
    PLANNER_KINDS,
    CallBudgetExceeded,
    PlannerSettingRefused,
    build_planner,
    check_call_budget,
)
from cellrt.planner.fixed_planner import FixedPlanner
from cellrt.planner.llm import DECIDE_TOOL, ChatRequest, LlmPlanner, LlmPlannerConfig
from cellrt.planner.nim import (
    BASE_URL_REFUSED,
    CAP_REACHED,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    KEY_ENV,
    LLM_EXTRA_MISSING,
    MAX_BASE_URL_CHARS,
    MAX_MODEL_CALLS_LIMIT,
    MISSING_KEY,
    SDK_ENV_VARS,
    BaseUrlRefused,
    LlmExtraMissing,
    LlmSetupError,
    MissingApiKey,
    ModelCallCap,
    ModelCallCapReached,
    NimChatClient,
    SdkEnvConflict,
    check_base_url,
    check_sdk_env,
    connect_openai,
    nim_client,
    read_key,
)
from cellrt.planner.rule_planner import RulePlanner
from cellrt.runtime.events import Event, PlannerResponded, RunStarted
from cellrt.runtime.store import EventStore

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
YAW = {s.id: s for s in load_scenarios(PHASE0)}["yaw_mismatch"]


def _request(*, tool: Mapping[str, object] | None = DECIDE_TOOL) -> ChatRequest:
    return ChatRequest(
        model=NIM_MODEL,
        messages=({"role": "system", "content": "s"}, {"role": "user", "content": "u"}),
        tool=tool,
        max_tokens=256,
        temperature=0.2,
    )


def _client(create: FakeCreate, *, cap: int = 10, **settings: Any) -> NimChatClient:
    return NimChatClient(create, cap=ModelCallCap(cap), **settings)


def _complete(client: NimChatClient, request: ChatRequest | None = None) -> Any:
    return asyncio.run(client.complete(request or _request()))


def _run(store: EventStore, scenario: Scenario, seed: int = 0, **kwargs: object) -> list[Event]:
    run = run_unit(scenario, "C", seed=seed, store=store, **kwargs)  # type: ignore[arg-type]
    return store.events(run.run_id)


def _types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def _rejections(events: list[Event]) -> list[list[str]]:
    return [e.payload.errors for e in events if e.type == "planner_output_rejected"]


# --- the request ---------------------------------------------------------------------------


def test_the_request_carries_model_messages_the_decide_tool_and_limits() -> None:
    create = FakeCreate([_completion()])

    _complete(_client(create))

    [sent] = create.calls
    assert set(sent) == {"model", "messages", "tools", "tool_choice", "max_tokens", "temperature"}
    assert (sent["model"], sent["max_tokens"], sent["temperature"]) == (NIM_MODEL, 256, 0.2)
    assert sent["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert sent["tools"] == [json.loads(json.dumps(DECIDE_TOOL))]
    assert sent["tools"][0]["function"]["name"] == "decide"
    assert sent["tool_choice"] == "auto"


def test_the_sdk_gets_its_own_copy_of_the_tool() -> None:
    create = FakeCreate([_completion()])

    _complete(_client(create))
    create.calls[0]["tools"][0]["function"]["name"] = "changed by the SDK"

    assert DECIDE_TOOL["function"]["name"] == "decide"  # type: ignore[index]


def test_tool_choice_is_a_client_setting() -> None:
    create = FakeCreate([_completion()])

    _complete(_client(create, tool_choice="required"))

    assert create.calls[0]["tool_choice"] == "required"
    with pytest.raises(ValueError):
        _client(FakeCreate([]), tool_choice="none")


def test_a_json_mode_request_sends_no_tool() -> None:
    create = FakeCreate([_completion(arguments=None, content=SWITCH_TO_ACT)])

    _complete(_client(create), _request(tool=None))

    assert "tools" not in create.calls[0] and "tool_choice" not in create.calls[0]


# --- the reply -----------------------------------------------------------------------------


def test_a_tool_call_maps_to_its_arguments() -> None:
    reply = _complete(_client(FakeCreate([_completion()])))

    assert reply.tool_arguments == SWITCH_TO_ACT
    assert reply.text is None
    assert (reply.prompt_tokens, reply.completion_tokens) == (812, 37)
    assert (reply.finish_reason, reply.model) == ("tool_calls", NIM_MODEL)


def test_a_text_answer_maps_to_its_content() -> None:
    text = _completion(arguments=None, content=SWITCH_TO_ACT, finish_reason="stop")

    reply = _complete(_client(FakeCreate([text])))

    assert (reply.tool_arguments, reply.text, reply.finish_reason) == (None, SWITCH_TO_ACT, "stop")


def test_missing_usage_maps_to_no_token_counts() -> None:
    reply = _complete(_client(FakeCreate([_completion(usage=None)])))

    assert (reply.prompt_tokens, reply.completion_tokens) == (None, None)
    assert reply.tool_arguments == SWITCH_TO_ACT


def test_missing_pieces_map_to_none_and_never_raise() -> None:
    no_choices = SimpleNamespace(model=NIM_MODEL, choices=[], usage=None)
    bare = SimpleNamespace()  # not even a model id: the request's is used
    no_message = SimpleNamespace(model=NIM_MODEL, choices=[SimpleNamespace()])
    empty_calls = _completion(arguments=None)
    empty_calls.choices[0].message.tool_calls = []
    no_function = _completion()
    del no_function.choices[0].message.tool_calls[0].function
    half_usage = _completion()
    del half_usage.usage.completion_tokens

    replies = [
        _complete(_client(FakeCreate([response])))
        for response in (no_choices, bare, no_message, empty_calls, no_function, half_usage)
    ]

    for reply in replies[:5]:
        assert (reply.tool_arguments, reply.text) == (None, None)
        assert reply.model == NIM_MODEL
    assert replies[1].finish_reason is None and replies[1].prompt_tokens is None
    assert (replies[5].prompt_tokens, replies[5].completion_tokens) == (812, None)


def test_describe_names_the_provider_and_host_and_never_the_key() -> None:
    client = _client(FakeCreate([]))
    local = _client(FakeCreate([]), base_url="http://[::1]:8000/v1", tool_choice="required")

    assert client.describe() == {
        "provider": "nim",
        "host": "integrate.api.nvidia.com",
        "tool_choice": "auto",
    }
    assert local.describe() == {"provider": "nim", "host": "::1", "tool_choice": "required"}


# --- the base URL --------------------------------------------------------------------------


def test_https_and_local_http_base_urls_are_accepted() -> None:
    accepted = {
        DEFAULT_BASE_URL: "integrate.api.nvidia.com",
        "https://Example.COM:8443/v1": "example.com",
        "http://localhost:8000/v1": "localhost",
        "http://127.0.0.1:8000/v1": "127.0.0.1",
        "http://[::1]:8000/v1": "::1",
    }
    for url, host in accepted.items():
        assert check_base_url(url) == host
    assert check_base_url("https://h/" + "v" * (MAX_BASE_URL_CHARS - 10)) == "h"


def test_any_other_base_url_is_refused_with_a_fixed_message() -> None:
    refused = [
        "http://integrate.api.nvidia.com/v1",  # plain http to a remote host
        "http://localhost.example.com/v1",  # not localhost
        "http://10.0.0.5:8000/v1",
        "ftp://integrate.api.nvidia.com/v1",
        "integrate.api.nvidia.com/v1",  # no scheme
        "https://",  # no host
        "",
        f"https://{FAKE_KEY}@integrate.api.nvidia.com/v1",  # credentials in the URL
        "https://user:pw@integrate.api.nvidia.com/v1",
        "https://integrate.api.nvidia.com:port/v1",  # a port that is not a number
        "http://[::1/v1",  # broken IPv6 literal
        "https://integrate.api.nvidia.com/v1\n",
        "https://h/" + "v" * (MAX_BASE_URL_CHARS - 9),  # one character too long
        None,
    ]
    for url in refused:
        with pytest.raises(BaseUrlRefused) as refusal:
            check_base_url(url)  # type: ignore[arg-type]
        assert str(refusal.value) == BASE_URL_REFUSED
        with pytest.raises(BaseUrlRefused):
            NimChatClient(FakeCreate([]), cap=ModelCallCap(1), base_url=url)  # type: ignore[arg-type]
    assert FAKE_KEY not in BASE_URL_REFUSED


# --- the key -------------------------------------------------------------------------------


def test_the_key_comes_from_nvidia_api_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert read_key({KEY_ENV: FAKE_KEY}) == FAKE_KEY
    assert read_key({KEY_ENV: f" {FAKE_KEY}\n"}) == FAKE_KEY  # pasted with a newline
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    assert read_key() == FAKE_KEY  # the process environment by default
    for env in ({}, {KEY_ENV: ""}, {KEY_ENV: "  "}, {"OPENAI_API_KEY": FAKE_KEY}):
        with pytest.raises(MissingApiKey) as missing:
            read_key(env)
        assert str(missing.value) == MISSING_KEY
    assert KEY_ENV in MISSING_KEY


# --- the call cap --------------------------------------------------------------------------


def test_the_call_after_the_cap_raises_before_any_request() -> None:
    create = FakeCreate([_completion(), _completion(), _completion()])
    client = _client(create, cap=2)

    _complete(client)
    _complete(client)
    with pytest.raises(ModelCallCapReached) as reached:
        _complete(client)

    assert len(create.calls) == 2
    assert str(reached.value) == CAP_REACHED


def test_clients_given_the_same_cap_share_it() -> None:
    cap = ModelCallCap(1)
    first = NimChatClient(FakeCreate([_completion()]), cap=cap)
    second_create = FakeCreate([_completion()])
    second = NimChatClient(second_create, cap=cap)

    _complete(first)
    with pytest.raises(ModelCallCapReached):
        _complete(second)

    assert second_create.calls == []
    assert (cap.used, cap.max_calls) == (1, 1)


def test_a_cap_of_zero_refuses_the_first_call() -> None:
    create = FakeCreate([_completion()])

    with pytest.raises(ModelCallCapReached):
        _complete(_client(create, cap=0))

    assert create.calls == []


def test_the_cap_is_bounded() -> None:
    ModelCallCap(MAX_MODEL_CALLS_LIMIT)
    for bad in (-1, MAX_MODEL_CALLS_LIMIT + 1, True, 1.5):
        with pytest.raises(ValueError):
            ModelCallCap(bad)  # type: ignore[arg-type]


def test_the_cap_is_reached_through_a_run(store: EventStore) -> None:
    create = FakeCreate([_completion(), _completion()])
    planner = build_planner(
        "llm", model=NIM_MODEL, max_model_calls=1, env={KEY_ENV: FAKE_KEY}, connect=_connect(create)
    )

    first = _run(store, YAW, planner=planner)
    second = _run(store, YAW, seed=1, planner=planner)

    assert first[-1].payload.outcome == "success"
    assert _rejections(second) == [["<root>: planner_raised ModelCallCapReached"]]
    assert "planner_responded" not in _types(second)
    assert "recovery_started" not in _types(second)
    assert len(create.calls) == 1  # the refused call sent nothing


# --- errors --------------------------------------------------------------------------------


class APIConnectionError(Exception):
    """Named like the SDK's error, with text that holds a key."""


def test_an_sdk_error_propagates_unchanged() -> None:
    error = APIConnectionError(f"connect failed for api_key={FAKE_KEY}")

    with pytest.raises(APIConnectionError) as raised:
        _complete(_client(FakeCreate([error])))

    assert raised.value is error


def test_an_sdk_error_is_recorded_by_its_type_only(store: EventStore) -> None:
    create = FakeCreate([APIConnectionError(f"connect failed for api_key={FAKE_KEY}")])
    planner = LlmPlanner(_client(create), LlmPlannerConfig(model=NIM_MODEL))

    events = _run(store, YAW, planner=planner)

    assert _rejections(events) == [["<root>: planner_raised APIConnectionError"]]
    assert events[-1].payload.outcome == "aborted"
    assert all(FAKE_KEY not in e.model_dump_json() for e in events)


# --- through a run -------------------------------------------------------------------------


def test_a_nim_backed_planner_recovers_the_yaw_mismatch(store: EventStore) -> None:
    create = FakeCreate([_completion()])
    planner = LlmPlanner(_client(create), LlmPlannerConfig(model=NIM_MODEL, max_tokens=512))

    events = _run(store, YAW, planner=planner)

    assert events[-1].payload.outcome == "success"
    [trace] = [e.payload for e in events if isinstance(e.payload, PlannerResponded)]
    assert (trace.provider, trace.model) == ("nim", NIM_MODEL)
    assert trace.response == SWITCH_TO_ACT
    assert (trace.prompt_tokens, trace.completion_tokens) == (812, 37)
    [sent] = create.calls
    assert sent["max_tokens"] == 512 and sent["tools"][0]["function"]["name"] == "decide"
    assert json.loads(sent["messages"][1]["content"])["rule"] == "insert_stalled"


def test_the_host_and_tool_choice_count_in_the_config_hash(store: EventStore) -> None:
    def started(**settings: Any) -> RunStarted:
        client = _client(FakeCreate([_completion()]), **settings)
        events = _run(store, YAW, planner=LlmPlanner(client, LlmPlannerConfig(model=NIM_MODEL)))
        assert isinstance(events[0].payload, RunStarted)
        return events[0].payload

    hosted = started()
    local = started(base_url="http://localhost:8000/v1")
    required = started(tool_choice="required")

    assert len({hosted.config_hash, local.config_hash, required.config_hash}) == 3


# --- the factory ---------------------------------------------------------------------------


def _connect(create: FakeCreate, seen: dict[str, object] | None = None) -> Any:
    def connect(*, base_url: str, api_key: str, timeout_s: float) -> FakeCreate:
        if seen is not None:
            seen.update(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
        return create

    return connect


def test_the_factory_builds_every_planner_kind_and_refuses_unknown_ones() -> None:
    assert PLANNER_KINDS == ("rule", "fixed", "llm")
    assert isinstance(build_planner("rule"), RulePlanner)
    assert isinstance(build_planner("fixed"), FixedPlanner)
    with pytest.raises(PlannerSettingRefused) as refused:
        build_planner("gpt")
    assert "gpt" not in str(refused.value)
    assert isinstance(refused.value, LlmSetupError) and isinstance(refused.value, ValueError)


def test_the_factory_builds_a_capped_nim_planner_from_its_settings() -> None:
    seen: dict[str, object] = {}

    planner = build_planner(
        "llm",
        model="nvidia/other-model",
        base_url="http://127.0.0.1:8000/v1",
        max_tokens=300,
        temperature=0.5,
        mode="json",
        max_model_calls=7,
        deadline_s=45.0,
        tool_choice="required",
        env={KEY_ENV: FAKE_KEY},
        connect=_connect(FakeCreate([]), seen),
    )

    assert isinstance(planner, LlmPlanner)
    assert planner.config == LlmPlannerConfig(
        model="nvidia/other-model", max_tokens=300, temperature=0.5, mode="json", deadline_s=45.0
    )
    assert planner.deadline_s == 45.0  # one deadline: the runtime's (ADR 016) and the SDK's
    assert planner.describe()["client"] == {
        "provider": "nim",
        "host": "127.0.0.1",
        "tool_choice": "required",
    }
    assert seen == {"base_url": "http://127.0.0.1:8000/v1", "api_key": FAKE_KEY, "timeout_s": 45.0}


def test_the_factory_defaults_to_the_smoke_tested_nemotron_on_hosted_nim() -> None:
    seen: dict[str, object] = {}

    planner = build_planner("llm", env={KEY_ENV: FAKE_KEY}, connect=_connect(FakeCreate([]), seen))

    assert isinstance(planner, LlmPlanner)
    # The model that answered the smoke call on 2026-11-09 (ADR 017).
    assert planner.config.model == DEFAULT_MODEL == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert seen["base_url"] == "https://integrate.api.nvidia.com/v1" == DEFAULT_BASE_URL
    assert seen["timeout_s"] == planner.deadline_s == 30.0


def test_the_sdk_timeout_has_the_planner_deadline_bounds() -> None:
    def client(timeout_s: float) -> NimChatClient:
        return nim_client(
            cap=ModelCallCap(1),
            timeout_s=timeout_s,
            env={KEY_ENV: FAKE_KEY},
            connect=_connect(FakeCreate([])),
        )

    client(MAX_PLANNER_DEADLINE_S)
    for bad in (0.0, -1.0, MAX_PLANNER_DEADLINE_S + 0.1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            client(bad)


@pytest.mark.parametrize(
    "setting",
    [
        {"deadline_s": MAX_PLANNER_DEADLINE_S + 1},
        {"deadline_s": 0.0},
        {"max_tokens": 0},
        {"temperature": -1.0},
        {"model": ""},
        {"model": "nvidia/model\x1b[2J"},  # an escape would reach the terminal and the log
        {"model": "nvidia/two words"},
        {"mode": "xml"},
        {"tool_choice": "none"},
        {"max_model_calls": MAX_MODEL_CALLS_LIMIT + 1},
        {"max_model_calls": -1},
    ],
)
def test_a_planner_setting_out_of_range_is_a_setup_error(setting: dict[str, Any]) -> None:
    create = FakeCreate([])

    with pytest.raises(PlannerSettingRefused):
        build_planner("llm", env={KEY_ENV: FAKE_KEY}, connect=_connect(create), **setting)

    assert create.calls == []


def test_planners_built_with_one_cap_share_it(store: EventStore) -> None:
    create = FakeCreate([_completion(), _completion()])
    cap = ModelCallCap(1)

    def planner() -> Any:
        return build_planner("llm", cap=cap, env={KEY_ENV: FAKE_KEY}, connect=_connect(create))

    first = _run(store, YAW, planner=planner())
    second = _run(store, YAW, seed=1, planner=planner())

    assert first[-1].payload.outcome == "success"
    assert _rejections(second) == [["<root>: planner_raised ModelCallCapReached"]]
    assert (cap.used, len(create.calls)) == (1, 1)


def test_a_missing_key_fails_before_the_sdk_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)  # the `llm` extra is not installed

    with pytest.raises(MissingApiKey) as missing:
        build_planner("llm", env={})
    with pytest.raises(LlmExtraMissing) as no_sdk:
        build_planner("llm", env={KEY_ENV: FAKE_KEY})

    assert str(missing.value) == MISSING_KEY
    assert str(no_sdk.value) == LLM_EXTRA_MISSING and "llm" in LLM_EXTRA_MISSING
    assert no_sdk.value.__cause__ is None and no_sdk.value.__suppress_context__


def test_a_refused_base_url_fails_before_the_key_is_read() -> None:
    with pytest.raises(BaseUrlRefused):
        build_planner("llm", base_url="http://example.com/v1", env={})


def test_setup_errors_share_one_type_for_the_cli() -> None:
    for error in (
        BaseUrlRefused,
        MissingApiKey,
        LlmExtraMissing,
        SdkEnvConflict,
        CallBudgetExceeded,
        PlannerSettingRefused,
    ):
        assert issubclass(error, LlmSetupError)


def test_the_call_budget_precheck_refuses_an_eval_too_big_for_the_cap() -> None:
    assert check_call_budget(10, 2, 20) == 20  # exactly at the cap
    assert check_call_budget(0, 2, 0) == 0
    assert check_call_budget(1, 1, MAX_MODEL_CALLS_LIMIT) == 1
    with pytest.raises(CallBudgetExceeded) as refused:
        check_call_budget(11, 2, 20)
    assert "22" in str(refused.value) and "20" in str(refused.value)
    bad_values = (
        (-1, 2, 20),
        (1, -1, 20),
        (1, 2, -1),
        (True, 2, 20),
        (1.5, 2, 20),
        (1, 1, MAX_MODEL_CALLS_LIMIT + 1),
    )
    for bad in bad_values:
        with pytest.raises(PlannerSettingRefused):
            check_call_budget(*bad)  # type: ignore[arg-type]


def test_the_call_budget_precheck_counts_one_task_plan_call_per_unit_planned() -> None:
    assert check_call_budget(2, 2, 10, plan_calls=6) == 10  # exactly at the cap
    assert check_call_budget(0, 2, 6, plan_calls=6) == 6  # plans only, no model triage
    with pytest.raises(CallBudgetExceeded) as refused:
        check_call_budget(2, 2, 9, plan_calls=6)
    assert "up to 10" in str(refused.value) and "plus 6 task plan calls" in str(refused.value)
    with pytest.raises(CallBudgetExceeded) as plans_only:
        check_call_budget(0, 2, 5, plan_calls=6)
    assert "(6 task plan calls)" in str(plans_only.value)  # no "0 units with a planner"
    for bad in (-1, True, 1.5):
        with pytest.raises(PlannerSettingRefused):
            check_call_budget(0, 2, 10, plan_calls=bad)  # type: ignore[arg-type]


# --- the default factory, with a stand-in `openai` module ----------------------------------


class _FakeAsyncOpenAI:
    """Records how the client is built and whether it was closed."""

    built: ClassVar[list[_FakeAsyncOpenAI]] = []
    fail_with: ClassVar[BaseException | None] = None
    hang: ClassVar[bool] = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.sent: dict[str, Any] = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        _FakeAsyncOpenAI.built.append(self)

    async def _create(self, **kwargs: Any) -> object:
        self.sent = kwargs
        if _FakeAsyncOpenAI.fail_with is not None:
            raise _FakeAsyncOpenAI.fail_with
        if _FakeAsyncOpenAI.hang:
            await asyncio.Event().wait()
        return _completion()

    async def __aenter__(self) -> _FakeAsyncOpenAI:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncOpenAI]:
    module = types.ModuleType("openai")
    module.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(_FakeAsyncOpenAI, "built", [])
    monkeypatch.setattr(_FakeAsyncOpenAI, "fail_with", None)
    monkeypatch.setattr(_FakeAsyncOpenAI, "hang", False)
    return _FakeAsyncOpenAI


def test_the_default_factory_builds_the_sdk_client_without_retries(
    fake_openai: type[_FakeAsyncOpenAI],
) -> None:
    create = connect_openai(base_url=DEFAULT_BASE_URL, api_key=FAKE_KEY, timeout_s=12.5)

    response = asyncio.run(create(model=NIM_MODEL, messages=[]))
    asyncio.run(create(model=NIM_MODEL, messages=[]))

    first, second = fake_openai.built  # one SDK client per call: each unit has its own loop
    assert first.kwargs == {
        "base_url": DEFAULT_BASE_URL,
        "api_key": FAKE_KEY,
        "max_retries": 0,
        "timeout": 12.5,
    }
    assert first.sent == {"model": NIM_MODEL, "messages": []}
    assert response.choices[0].finish_reason == "tool_calls"
    assert first.closed and second.closed


def test_the_sdk_client_is_closed_when_the_call_fails_or_is_cancelled(
    fake_openai: type[_FakeAsyncOpenAI],
) -> None:
    create = connect_openai(base_url=DEFAULT_BASE_URL, api_key=FAKE_KEY, timeout_s=1.0)
    fake_openai.fail_with = APIConnectionError("down")
    with pytest.raises(APIConnectionError):
        asyncio.run(create(model=NIM_MODEL, messages=[]))
    fake_openai.fail_with = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(create(model=NIM_MODEL, messages=[]))

    assert [c.closed for c in fake_openai.built] == [True, True]


def test_without_the_sdk_the_default_factory_says_to_install_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)

    with pytest.raises(LlmExtraMissing) as missing:
        connect_openai(base_url=DEFAULT_BASE_URL, api_key=FAKE_KEY, timeout_s=1.0)

    assert str(missing.value) == LLM_EXTRA_MISSING


# --- the key goes to the SDK and nowhere else ----------------------------------------------


def test_the_key_reaches_the_sdk_and_nothing_else(store: EventStore) -> None:
    seen: dict[str, object] = {}
    create = FakeCreate([_completion(), APIConnectionError(f"bad key {FAKE_KEY}")])
    planner = build_planner(
        "llm", model=NIM_MODEL, env={KEY_ENV: FAKE_KEY}, connect=_connect(create, seen)
    )

    events = _run(store, YAW, planner=planner) + _run(store, YAW, seed=1, planner=planner)

    assert seen["api_key"] == FAKE_KEY  # the key is really used
    assert all(FAKE_KEY not in e.model_dump_json() for e in events)
    started = [e.payload for e in events if isinstance(e.payload, RunStarted)]
    assert all(FAKE_KEY not in json.dumps(s.config_parts) + s.config_hash for s in started)
    assert FAKE_KEY not in json.dumps(planner.describe())  # what the config hash is made of
    assert FAKE_KEY not in repr(planner) and FAKE_KEY not in repr(vars(planner))
    assert FAKE_KEY not in json.dumps([str(c) for c in create.calls])


def test_no_setup_or_cap_message_holds_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)
    attempts = [
        lambda: build_planner("llm", base_url=f"https://{FAKE_KEY}@h/v1", env={KEY_ENV: FAKE_KEY}),
        lambda: build_planner("llm", base_url=f"http://h/?key={FAKE_KEY}", env={KEY_ENV: FAKE_KEY}),
        lambda: build_planner("llm", env={"NVIDIA_KEY": FAKE_KEY}),
        lambda: build_planner("llm", env={KEY_ENV: FAKE_KEY}),  # no SDK
        lambda: build_planner("llm", model=FAKE_KEY * 20, env={KEY_ENV: FAKE_KEY}),  # too long
        lambda: _complete(_client(FakeCreate([]), cap=0)),
        lambda: check_call_budget(100, 2, 1),
    ]
    messages = []
    for attempt in attempts:
        with pytest.raises(Exception) as raised:
            attempt()
        messages.append(f"{raised.value!s} {raised.value!r}")

    assert len(messages) == len(attempts)
    assert all(FAKE_KEY not in m for m in messages)


def test_a_call_cancelled_while_it_waits_closes_the_sdk_client(
    fake_openai: type[_FakeAsyncOpenAI],
) -> None:
    create = connect_openai(base_url=DEFAULT_BASE_URL, api_key=FAKE_KEY, timeout_s=1.0)
    fake_openai.hang = True

    async def cancel_mid_wait() -> None:
        task = asyncio.ensure_future(create(model=NIM_MODEL, messages=[]))
        loop = asyncio.get_running_loop()
        for _ in range(3):  # let the call reach its wait, without a timer
            turn = loop.create_future()
            loop.call_soon(turn.set_result, None)
            await turn
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_mid_wait())

    [client] = fake_openai.built
    assert client.closed


# --- what a reply may hold -----------------------------------------------------------------

RETRY = '{"kind": "retry"}'


def _with_calls(*calls: tuple[str, str]) -> SimpleNamespace:
    response = _completion()
    response.choices[0].message.tool_calls = [
        SimpleNamespace(
            id=f"call_{i}", type="function", function=SimpleNamespace(name=name, arguments=args)
        )
        for i, (name, args) in enumerate(calls)
    ]
    return response


def test_several_decide_calls_go_on_whole_and_are_never_picked_from() -> None:
    reply = _complete(
        _client(FakeCreate([_with_calls(("decide", RETRY), ("decide", SWITCH_TO_ACT))]))
    )

    assert json.loads(reply.tool_arguments) == [
        {"name": "decide", "arguments": RETRY},
        {"name": "decide", "arguments": SWITCH_TO_ACT},
    ]


def test_a_call_to_another_function_is_not_an_answer() -> None:
    reply = _complete(_client(FakeCreate([_with_calls(("shell", SWITCH_TO_ACT))])))

    assert json.loads(reply.tool_arguments) == [{"name": "shell", "arguments": SWITCH_TO_ACT}]


def test_in_json_mode_a_tool_call_is_ignored_and_the_text_is_the_answer() -> None:
    response = _completion(content=RETRY)

    reply = _complete(_client(FakeCreate([response])), _request(tool=None))

    assert (reply.tool_arguments, reply.text) == (None, RETRY)


@pytest.mark.parametrize(
    "calls",
    [(("decide", RETRY), ("decide", SWITCH_TO_ACT)), (("shell", SWITCH_TO_ACT),)],
    ids=["two decide calls", "another function"],
)
def test_through_a_run_a_reply_that_is_not_one_decide_call_is_rejected_and_traced(
    store: EventStore, calls: tuple[tuple[str, str], ...]
) -> None:
    planner = LlmPlanner(
        _client(FakeCreate([_with_calls(*calls)])), LlmPlannerConfig(model=NIM_MODEL)
    )

    events = _run(store, YAW, planner=planner)

    assert "planner_decided" not in _types(events) and len(_rejections(events)) == 1
    [trace] = [e.payload for e in events if isinstance(e.payload, PlannerResponded)]
    assert [c["name"] for c in json.loads(trace.response)] == [name for name, _ in calls]
    assert events[-1].payload.outcome == "aborted"


@pytest.mark.parametrize(
    ("usage", "finish_reason", "model"),
    [((812.0, 37), "tool_calls", NIM_MODEL), ((-1, "37"), 7, None), ((True, None), None, 42)],
    ids=["float tokens", "negative and text tokens, odd finish", "bool tokens, odd model"],
)
def test_odd_bookkeeping_from_a_host_never_costs_a_valid_decision(
    store: EventStore, usage: tuple[object, object], finish_reason: object, model: object
) -> None:
    response = _completion()
    response.usage.prompt_tokens, response.usage.completion_tokens = usage
    response.choices[0].finish_reason = finish_reason
    response.model = model
    planner = LlmPlanner(_client(FakeCreate([response])), LlmPlannerConfig(model=NIM_MODEL))

    events = _run(store, YAW, planner=planner)

    assert "planner_decided" in _types(events)
    assert events[-1].payload.outcome == "success"
    [trace] = [e.payload for e in events if isinstance(e.payload, PlannerResponded)]
    assert trace.model == NIM_MODEL  # the requested one, when the host's is not a name
    assert trace.prompt_tokens is None or trace.prompt_tokens == usage[0]
    assert trace.finish_reason in (None, "tool_calls")


# --- the SDK's own environment variables -----------------------------------------------------


@pytest.mark.parametrize("name", SDK_ENV_VARS)
def test_the_client_refuses_to_start_while_the_sdk_would_send_an_openai_setting(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    for other in SDK_ENV_VARS:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "org-PRIVATE-value")

    with pytest.raises(SdkEnvConflict) as refused:
        nim_client(cap=ModelCallCap(1), env={KEY_ENV: FAKE_KEY}, connect=_connect(FakeCreate([])))

    assert name in str(refused.value)
    assert "PRIVATE" not in str(refused.value) and "PRIVATE" not in repr(refused.value)
    assert isinstance(refused.value, LlmSetupError)


def test_an_empty_sdk_variable_is_no_conflict() -> None:
    check_sdk_env({name: "" for name in SDK_ENV_VARS})
    check_sdk_env({})
