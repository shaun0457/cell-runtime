"""A chat client for NVIDIA NIM's OpenAI-compatible API (ADR 017).

`NimChatClient` implements the core's `ChatClient`. It is handed an async create call
(`connect_openai`'s, which runs the SDK's `OpenAI().chat.completions.create` on a thread, or a
test's fake) and maps a `ChatRequest` to that
call's keyword arguments and the SDK's response back to a `ChatReply`. Only `connect_openai`
imports `openai`, inside the function: the SDK is the optional extra `llm`, and the core imports
without it.

Keys and errors: the key is read from `NVIDIA_API_KEY` only and handed to the SDK; it is kept in
no event, no `describe()` and no message. Every error raised here has a fixed message. Errors
from the SDK propagate unchanged, and the runtime records only their type name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from cellrt.planner.base import DEFAULT_PLANNER_DEADLINE_S, checked_deadline
from cellrt.planner.llm import ChatReply, ChatRequest

PROVIDER = "nim"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Answered the smoke call through the `decide` tool on 2026-11-09 (ADR 017). Nemotron 3 Super
# was listed by GET {DEFAULT_BASE_URL}/models but answered 404 for the account used.
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
KEY_ENV = "NVIDIA_API_KEY"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_BASE_URL_CHARS = 512
MAX_MODEL_CALLS_LIMIT = 10_000  # the largest cap an eval may set
ToolChoice = Literal["auto", "required"]
TOOL_CHOICES: tuple[str, ...] = ("auto", "required")
REQUEST_THREAD = "nim-request"  # the name of each request's thread (`connect_openai`)
# Request threads one client may have at once: calls go one at a time, so the others are
# cancelled calls whose requests are still running.
MAX_LIVE_REQUESTS = 4

BASE_URL_REFUSED = "the base URL must be https, or http to localhost, 127.0.0.1 or ::1"
MISSING_KEY = f"{KEY_ENV} is not set: export your NVIDIA API key in it to use the model planner"
LLM_EXTRA_MISSING = (
    "the model planner needs the optional `llm` extra: run `uv sync --extra llm` "
    "(or `pip install 'cell-runtime[llm]'`)"
)
CAP_REACHED = "the model call cap is reached: no request was sent"
REQUESTS_BUSY = f"{MAX_LIVE_REQUESTS} earlier model requests are still running: no request was sent"
REQUEST_LOST = "the model request ended without an answer"
# Read by the openai SDK from the process environment and sent to whatever host the client
# talks to: an OpenAI organisation or project id would go to NVIDIA, and custom headers could
# replace the Authorization header. The client refuses to start while any is set.
SDK_ENV_VARS = (
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_ADMIN_KEY",
    "OPENAI_WEBHOOK_SECRET",
)

CreateFn = Callable[..., Awaitable[Any]]


class LlmSetupError(Exception):
    """A model planner that cannot be set up. The message is fixed and safe to print."""


class BaseUrlRefused(LlmSetupError):
    def __init__(self) -> None:
        super().__init__(BASE_URL_REFUSED)


class MissingApiKey(LlmSetupError):
    def __init__(self) -> None:
        super().__init__(MISSING_KEY)


class LlmExtraMissing(LlmSetupError):
    def __init__(self) -> None:
        super().__init__(LLM_EXTRA_MISSING)


class SdkEnvConflict(LlmSetupError):
    def __init__(self, names: list[str]) -> None:
        # Names from the fixed list SDK_ENV_VARS only, never their values.
        super().__init__(
            f"unset {', '.join(names)} before calling NIM: the openai SDK would send it to NVIDIA"
        )


class ModelCallCapReached(RuntimeError):
    """The call after the cap. Raised before any request is made; a run records it as
    `planner_raised ModelCallCapReached`."""

    def __init__(self) -> None:
        super().__init__(CAP_REACHED)


class RequestsBusy(RuntimeError):
    """A call while MAX_LIVE_REQUESTS requests of earlier, cancelled calls still run. Raised
    before any request is made, and only then: `NimChatClient` gives the call back to the cap."""

    def __init__(self) -> None:
        super().__init__(REQUESTS_BUSY)


class RequestLost(RuntimeError):
    """The request's thread ended on something other than an answer or an `Exception`."""

    def __init__(self) -> None:
        super().__init__(REQUEST_LOST)


class Connect(Protocol):
    """Builds the SDK's create call for one provider endpoint (`connect_openai`, or a fake)."""

    def __call__(self, *, base_url: str, api_key: str, timeout_s: float) -> CreateFn: ...


class ModelCallCap:
    """The most model calls one eval (one process) may make, shared by every client given it.

    `take` is called before each request. Each eval unit runs its own event loop, one after the
    other, so a plain counter suffices.
    """

    def __init__(self, max_calls: int) -> None:
        if (
            isinstance(max_calls, bool)
            or not isinstance(max_calls, int)
            or not 0 <= max_calls <= MAX_MODEL_CALLS_LIMIT
        ):
            raise ValueError(f"max model calls must be an integer in [0, {MAX_MODEL_CALLS_LIMIT}]")
        self.max_calls = max_calls
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    def take(self) -> None:
        if self._used >= self.max_calls:
            raise ModelCallCapReached()
        self._used += 1

    def give_back(self) -> None:
        """Return the last call taken: it was refused before it sent anything."""
        if self._used == 0:
            raise ValueError("no call to give back")
        self._used -= 1


def check_base_url(url: str) -> str:
    """The host of an allowed base URL: https, or http to this machine (a local NIM). Anything
    else is refused with a fixed message that never echoes the URL, which may hold a secret."""
    if not isinstance(url, str) or not 0 < len(url) <= MAX_BASE_URL_CHARS:
        raise BaseUrlRefused()
    if any(c.isspace() or not c.isprintable() for c in url):
        raise BaseUrlRefused()
    try:
        parts = urlsplit(url)
        host = parts.hostname  # lower-cased, IPv6 brackets removed
        parts.port  # noqa: B018 (raises ValueError on a port that is not a number)
    except ValueError:
        raise BaseUrlRefused() from None
    if not host or "@" in parts.netloc:  # user info in the URL: credentials have no place here
        raise BaseUrlRefused()
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS):
        return host
    raise BaseUrlRefused()


def read_key(env: Mapping[str, str] | None = None) -> str:
    """The API key from `NVIDIA_API_KEY` (the process environment by default), and from nowhere
    else. Missing or blank: a fixed message that names the variable, never a value."""
    source = os.environ if env is None else env
    key = source.get(KEY_ENV)
    if not isinstance(key, str) or not key.strip():
        raise MissingApiKey()
    return key.strip()


def check_sdk_env(environ: Mapping[str, str] | None = None) -> None:
    """Refuse while the process environment sets a variable the SDK would send along."""
    source = os.environ if environ is None else environ
    if names := [n for n in SDK_ENV_VARS if source.get(n)]:
        raise SdkEnvConflict(names)


def check_tool_choice(tool_choice: str) -> None:
    # "required" forces a tool call where the host supports it; whether NIM does for a given
    # model is what the smoke call finds out (ADR 017).
    if tool_choice not in TOOL_CHOICES:
        raise ValueError(f"tool_choice must be one of {', '.join(TOOL_CHOICES)}")


def connect_openai(*, base_url: str, api_key: str, timeout_s: float) -> CreateFn:
    """The SDK's create call, through the sync `openai.OpenAI` with no retries and `timeout_s`
    as its timeout, made on a thread of its own. The only place `openai` is imported; without
    it, a fixed message says to install the `llm` extra.

    The thread lets the request go on while the loop is blocked in a world step: a live sim's
    `AicWorld.step` blocks it for a step of wall time, and an async request could only move
    while the loop was free, so each network round trip waited out a step (ADR 020). The loop
    reads the answer when it is back. The world stays single-threaded (ADR 003): only the HTTP
    request runs on the thread, and its result reaches the loop through `call_soon_threadsafe`.

    A thread cannot be cancelled. A cancelled call ends at once; its request runs on until it
    is answered or the SDK's timeout ends it, and its answer is dropped. The SDK applies that
    timeout to each network phase (connect, each read), not to the whole request, so a host
    that sends a byte now and then keeps a request alive with no bound in time. The bound is on
    how many: while MAX_LIVE_REQUESTS requests run, a call is refused (`RequestsBusy`) before
    it sends anything. The await has no deadline of its own: every caller sets the runtime's
    (ADR 016). The thread is a daemon, so a run whose loop has closed, or the process, does not
    wait for it."""
    try:
        import openai  # the optional `llm` extra (ADR 017); never at the core's import time
    except ImportError:
        raise LlmExtraMissing() from None
    live = threading.BoundedSemaphore(MAX_LIVE_REQUESTS)

    async def create(**arguments: Any) -> Any:
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[Any] = loop.create_future()

        def deliver(outcome: Callable[[Any], None], value: Any) -> None:
            if not answer.done():  # a cancelled call's answer is dropped
                outcome(value)

        given_back = threading.Lock()

        def give_back() -> None:
            """Return the call's slot once: a start that fails after the thread began (Ctrl+C
            while `start` waits) would return it twice, which BoundedSemaphore raises on."""
            if given_back.acquire(blocking=False):
                live.release()

        def request() -> None:
            # One SDK client per call, closed on every exit path: the request's thread owns it.
            reply: Any = None
            result: tuple[Callable[[Any], None], Any] = (answer.set_exception, RequestLost())
            try:
                with openai.OpenAI(
                    base_url=base_url, api_key=api_key, max_retries=0, timeout=timeout_s
                ) as client:
                    reply = client.chat.completions.create(**arguments)
                    result = (answer.set_result, reply)
            except Exception as exc:  # handed to the caller as it is, as the SDK raised it
                if reply is None:  # a close that fails after the answer does not lose it
                    result = (answer.set_exception, exc)
            except BaseException:  # nobody above a thread to hand it to: the caller is told
                pass
            finally:
                give_back()
                with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits
                    loop.call_soon_threadsafe(deliver, *result)

        if not live.acquire(blocking=False):
            raise RequestsBusy()
        try:
            threading.Thread(target=request, name=REQUEST_THREAD, daemon=True).start()
        except BaseException:
            give_back()
            raise
        return await answer

    return create


class NimChatClient:
    """`ChatClient` for NIM: one chat completion per call, counted against `cap`."""

    def __init__(
        self,
        create: CreateFn,
        *,
        cap: ModelCallCap,
        base_url: str = DEFAULT_BASE_URL,
        tool_choice: ToolChoice = "auto",
    ) -> None:
        check_tool_choice(tool_choice)
        self._tool_choice = tool_choice
        self._host = check_base_url(base_url)
        self._create = create
        self._cap = cap

    def describe(self) -> Mapping[str, str]:
        """Hashed into the run's configuration: who answers and how the tool is offered."""
        return {"provider": PROVIDER, "host": self._host, "tool_choice": self._tool_choice}

    async def complete(self, request: ChatRequest) -> ChatReply:
        self._cap.take()
        try:
            response = await self._create(**self._arguments(request))
        except RequestsBusy:
            self._cap.give_back()  # refused before it sent anything: not a call (#66 review F2)
            raise
        return _reply(response, request.model, _tool_name(request.tool))

    def _arguments(self, request: ChatRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(m) for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tool is not None:
            # Plain JSON, and the SDK's own copy: the planner's tool is shared by every call.
            arguments["tools"] = [json.loads(json.dumps(request.tool))]
            arguments["tool_choice"] = self._tool_choice
        if request.reasoning == "off":
            # The switch Nemotron's chat template reads on NIM, sent in the body by the SDK:
            # the smoke call of 2026-11-10 answered in 26 completion tokens, not 844 (ADR 017).
            # A model whose template has no such switch ignores it.
            arguments["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return arguments


def _reply(response: object, requested_model: str, tool_name: str | None) -> ChatReply:
    """The first choice's call to the requested tool and its text, the usage and the finish
    reason. A missing piece is None, never an error. The bookkeeping fields (model, tokens,
    finish reason) are kept only when their type is right, so an odd value from a host never
    costs a valid decision; the answer itself goes on as it came, to validation."""
    choices = getattr(response, "choices", None) or ()
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    calls = list(getattr(message, "tool_calls", None) or ())
    usage = getattr(response, "usage", None)
    model = getattr(response, "model", None)
    return ChatReply(
        model=model if isinstance(model, str) and model else requested_model,
        tool_arguments=_decide_arguments(calls, tool_name),
        text=getattr(message, "content", None),
        prompt_tokens=_count(getattr(usage, "prompt_tokens", None)),
        completion_tokens=_count(getattr(usage, "completion_tokens", None)),
        finish_reason=_text(getattr(choice, "finish_reason", None)),
    )


def _decide_arguments(calls: list[object], tool_name: str | None) -> Any:
    """The arguments of the one call to the requested tool. Several calls, or a call to another
    function, are an answer the runtime must not pick from: every call goes on as one JSON
    list, which validation rejects and the trace keeps. Without a requested tool (JSON mode),
    calls are ignored and the text is the answer."""
    if tool_name is None or not calls:
        return None
    functions = [getattr(call, "function", None) for call in calls]
    if all(f is None for f in functions):
        return None
    if len(functions) == 1 and getattr(functions[0], "name", None) == tool_name:
        return getattr(functions[0], "arguments", None)
    return json.dumps(
        [
            {
                "name": _text(getattr(f, "name", None)),
                "arguments": _text(getattr(f, "arguments", None)),
            }
            for f in functions
        ]
    )


def _tool_name(tool: Mapping[str, object] | None) -> str | None:
    function = tool.get("function") if tool is not None else None
    name = function.get("name") if isinstance(function, Mapping) else None
    return name if isinstance(name, str) else None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def nim_client(
    *,
    cap: ModelCallCap,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_PLANNER_DEADLINE_S,
    tool_choice: ToolChoice = "auto",
    env: Mapping[str, str] | None = None,
    connect: Connect = connect_openai,
) -> NimChatClient:
    """A NIM client set up from settings and the environment. Checked in this order, each with
    a fixed message: the base URL, the SDK timeout (the planner's deadline, same bounds), the
    key, the SDK's own environment variables (SDK_ENV_VARS), then the SDK. A missing key is
    reported even where the `llm` extra is not installed."""
    check_base_url(base_url)
    timeout_s = checked_deadline(timeout_s)
    check_tool_choice(tool_choice)
    key = read_key(env)
    check_sdk_env()  # the SDK reads the process environment, not `env`
    create = connect(base_url=base_url, api_key=key, timeout_s=timeout_s)
    return NimChatClient(create, cap=cap, base_url=base_url, tool_choice=tool_choice)
