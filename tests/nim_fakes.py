"""Stand-ins for the openai SDK's `create` call and its responses, for the NIM client tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

SWITCH_TO_ACT = '{"kind": "switch_skill", "skill": "insert_act"}'
NIM_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
# Shaped like a real key; never a real one.
FAKE_KEY = "nvapi-FAKEk3y0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"


def completion(
    *,
    arguments: str | None = SWITCH_TO_ACT,
    content: str | None = None,
    usage: tuple[int, int] | None = (812, 37),
    finish_reason: str | None = "tool_calls",
    model: str | None = NIM_MODEL,
    name: str = "decide",
) -> SimpleNamespace:
    """A response shaped like the SDK's `ChatCompletion`: a call to the tool `name`."""
    tool_calls = (
        None
        if arguments is None
        else [
            SimpleNamespace(
                id="call_0",
                type="function",
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ]
    )
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        id="chatcmpl-0",
        object="chat.completion",
        model=model,
        choices=[SimpleNamespace(index=0, message=message, finish_reason=finish_reason)],
        usage=None
        if usage is None
        else SimpleNamespace(
            prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=sum(usage)
        ),
    )


@dataclass
class FakeCreate:
    """Stands in for the async create call `connect_openai` builds: answers in order and
    records the keyword arguments of every call."""

    responses: list[object]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response
