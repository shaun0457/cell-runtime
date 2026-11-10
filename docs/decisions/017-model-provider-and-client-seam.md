# 017: Model provider and the client seam

Date: 2026-11-09
Status: accepted

## Context

Phase 3's first path (#44) lets a model triage the failures the Monitor cannot classify. #45
gave the core its own chat-client protocol (`ChatClient`, `ChatRequest`, `ChatReply` in
`cellrt/planner/llm.py`) and a model planner on top of it. This record picks the provider, the
code that reaches it, how the key is handled and how the model answers (#49).

Constraints:

- The core imports no vendor SDK at import time. `tests/import_core_without_ros.py` blocks
  `openai` and `anthropic` the way it blocks ROS.
- CI and the base install run with no network, no key and no SDK.
- No key and no provider text may reach the event log or the terminal. An SDK error can carry
  request details or part of a key, and the scrubber is best effort.
- A mistake must not run up a bill, so calls are capped per eval.
- The interview is about 2026-11-16, so the provider has to work this week.
- What this record relies on about NIM: an OpenAI-compatible chat API at
  `https://integrate.api.nvidia.com/v1`, function tools in that API's format, and a model list
  at `GET /v1/models`. These are what NVIDIA documents; the smoke call (below) is what checks
  them for the account and model used. A listed model is not always callable: see the smoke
  result.

## Options

### Provider

#### Option A — NVIDIA NIM, hosted (`integrate.api.nvidia.com`), OpenAI-compatible
- Pros: free credits for development; Nemotron models; no local GPU; the same API as a local
  NIM; fits the NVIDIA stack of the target role.
- Cons: rate limits on the free tier; tool calling support differs per model; the hosted
  catalogue changes; needs the network.

#### Option B — a local NIM container (for example on the Phase 2 T4 VM)
- Pros: no cost per call; no data leaves the machine; the same API as the hosted one.
- Cons: a T4 (16 GB) cannot hold a 120B model, so only small models; setup time this week.

#### Option C — another OpenAI-compatible host (OpenAI, Together, a vLLM server)
- Pros: tool calling is well tested on the larger hosts.
- Cons: another account and key; less related to the target role.

#### Option D — a vendor SDK per provider (`openai`, `anthropic`, ...)
- Pros: each provider's full feature set.
- Cons: one client per provider to write and test; more dependencies; this slice needs one
  chat call.

### Client seam

#### Option A — the core's own protocol; the SDK only behind a lazily imported factory
- Pros: the core imports and tests without the SDK; the provider can change; the request and
  response mapping is tested against a fake `create`.
- Cons: a thin mapping layer to keep up to date; anything outside the protocol (streaming,
  reasoning text, `response_format`) needs a protocol change.

#### Option B — SDK types in the core
- Pros: less code.
- Cons: the core needs the SDK to import; tests need SDK objects; tied to one provider's types.

#### Option C — an agent framework (LangGraph, NeMo Agent Toolkit)
- Pros: tracing, retries and tool loops come built in.
- Cons: a large dependency for one call; its own retries and state hide what this runtime has
  to control and show (the deadline, the cap, the audit trail); out of scope in #44.

### Key handling

#### Option A — the environment variable `NVIDIA_API_KEY` only
- Pros: usual practice; never in a file of the repo; CI never has it.
- Cons: child processes can read it; the user must export it.

#### Option B — a key or config file
- Pros: set once.
- Cons: can be committed by mistake; one more file to protect.

#### Option C — a command-line option
- Pros: explicit.
- Cons: lands in the shell history and the process list.

### Answer format

#### Option A — tool calling: the `decide` tool, whose parameters are the decision schema
- Pros: structured arguments; models trained for tools follow a schema better.
- Cons: support differs per model and per host; `tool_choice: "required"` is not supported
  everywhere.

#### Option B — JSON mode: the schema in the prompt, one JSON object as the text
- Pros: works with any chat model.
- Cons: more malformed answers; prose around the JSON.

Both go through the same validator (ADR 008), so the choice changes how often an answer is
valid, not what an invalid answer can do.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Shaun chose NVIDIA NIM through the `openai` SDK on 2026-11-09.

- **Provider:** NIM's hosted API (provider A), through the `openai` SDK, installed as the
  optional extra `llm` (`openai>=3.46,<4`). The default model is Nemotron 3.5 Lightning
  (`nvidia/nemotron-3.5-lightning-30b-a3b`), the one that answered the smoke call; Nemotron 3
  Super was the first choice but is not callable from this account. A local NIM works through
  the same client.
- **Seam:** the core's own `ChatClient` (seam A). `NimChatClient` (`cellrt/planner/nim.py`)
  takes the SDK's `create` call as an argument. Only `connect_openai` imports `openai`, inside
  the function. It builds one `AsyncOpenAI` per call, with `max_retries=0`, and closes it on
  every exit path.
- **One deadline:** the planner's `deadline_s` (ADR 016, default 30 s, at most 120 s) is also
  the SDK's timeout; there is no second setting. The SDK applies its timeout to each network
  phase (connect, each read), so it is not a bound on the whole call: that bound is the
  runtime's, which cancels the call at the deadline.
- **Base URL:** `https://integrate.api.nvidia.com/v1` by default. Only https, or http to
  `localhost`, `127.0.0.1` or `::1`. No user info in the URL. The refusal is a fixed message
  that does not repeat the URL.
- **Key:** read from `NVIDIA_API_KEY` only (key option A), when the planner is set up. A
  missing key is a fixed message that names the variable. It is checked before the SDK, so a
  missing key is reported even without the extra. The key goes to the SDK and nowhere else:
  not into `describe()`, the config hash, an event, an exception message or a report.
- **The SDK's own variables:** the client refuses to start while the process sets one the
  SDK would send along to NVIDIA (`SDK_ENV_VARS`: `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`,
  `OPENAI_CUSTOM_HEADERS`, `OPENAI_ADMIN_KEY`, `OPENAI_WEBHOOK_SECRET`). The message names the
  variables, never their values (`SdkEnvConflict`).
- **Cap:** one `ModelCallCap` per command, that is per process, shared by every planner the
  command builds. The call after the cap raises `ModelCallCapReached` before any request; the
  run records `planner_raised ModelCallCapReached`. Before an eval starts,
  `check_call_budget` refuses it when the units of setting C × the per-task planner budget
  exceed the cap. That count includes units a resumed eval will skip, so it errs high.
- **Command line:** `cellrt eval` and `cellrt run` take `--planner llm`, `--model` and
  `--max-model-calls` (default 100, at most 10 000). `cellrt smoke-llm` makes the smoke call
  with a cap of 1 and prints its report as JSON. A command without setting C builds no planner
  and needs no key. Every setup error is an `LlmSetupError` with a fixed message, printed as
  is.
- **Errors:** SDK exceptions propagate unchanged. The executor and the smoke report keep their
  type name only.
- **Answer:** tool calling first (format A), with `tool_choice: "auto"`. `"required"` is a
  client setting, counted in the config hash, to try in the smoke call. JSON mode
  (`mode="json"`) is the fallback if the smoke call shows tool calling does not work.

## Why

- **NIM:** the target role builds agents on NVIDIA's robotics stack, so NIM and Nemotron are
  the provider and models it would use; it is free for development, and it speaks the OpenAI
  API. The same client reaches the hosted API, a local NIM or another compatible host;
  a switch is a base URL and a model id.
- **The `openai` SDK:** it is the client NIM documents. It handles HTTP, errors and response
  types. As an optional extra it stays out of the core and out of CI.
- **Own seam:** the core stays testable without the SDK or the network, and every part of the
  mapping is tested against a fake. A framework would add retries and state that hide the
  deadline, the cap and the audit trail, which are what this project sets out to show.
- **One SDK client per call:** each eval unit runs in its own event loop (`asyncio.run`), and
  an HTTP connection pool must not outlive the loop that opened it. One TLS handshake per call
  is small next to a model's latency.
- **No retries in the SDK:** a hidden retry doubles the wait and the cost without the runtime
  knowing. The runtime decides what follows a failed call: under ADR 008 it is a rejection.
- **Key from the environment only:** the usual practice. It keeps the key out of the repo, the
  shell history and the log.
- **Fixed messages and type names:** the scrubber is best effort, so the rule is to never
  record provider text, not to clean it.

## Consequences

- `uv sync --extra llm` (or `pip install 'cell-runtime[llm]'`) is needed to call a model. The
  base `uv sync` and CI do not install `openai`.
- A new major version of `openai` means checking the mapping again; the pin stops at `<4`.
- The lock file now lists `openai` and its dependencies, so the `lock` part of every run's
  config hash changes once (`code` changes with every commit anyway).
- A provider feature outside `ChatRequest` / `ChatReply` (streaming, reasoning text,
  `response_format`) needs a protocol change.
- The SDK reads more of the environment than the refused variables. `OPENAI_LOG` turns on its
  logging: leave it unset for a real eval. Its HTTP client honours the proxy environment
  variables and follows the host's redirects, so a proxy set in the environment sees the
  requests. This is documented, not enforced.
- Any https host is accepted as the base URL, and the key is sent to it. Pointing
  `base_url` at a host is trusting it with the key.
- The cap counts calls, not tokens; `max_tokens` bounds each call. It is not kept across
  processes: a resumed eval starts a new count.
- A rate-limited or failed call is recorded as `planner_raised <type>` (for example
  `RateLimitError`) and the step fails without running anything.
- The SDK's timeout bounds each network wait. The wall-time deadline on the whole call, with a
  frozen sim clock while it runs, is ADR 016's (#47).

## Smoke result

One call by hand sends the yaw-mismatch UNKNOWN through the planner (`cellrt smoke-llm`, which
runs `cellrt.planner.smoke.run_smoke`), hosted NIM, tool mode, `tool_choice: "auto"`,
2026-11-09.

| Model | Result |
|---|---|
| `nvidia/nemotron-3.5-lightning-30b-a3b` | Answered through the `decide` tool: `switch_skill` to `insert_act`, the answer that works for this failure. Valid. Finish reason `tool_calls`. |
| `nvidia/nemotron-3-super-120b-a12b` | HTTP 404 Not Found: the model's function is "Not found for account", with a key that works for Lightning. |
| `nvidia/llama-3.1-nemotron-ultra-253b-v1`, `nvidia/llama-3.1-nemotron-70b-instruct` | HTTP 404, the same answer. |

Two Lightning calls, before and after #48 added `escalate` to the prompt: 8.69 s with 1061
prompt and 597 completion tokens; then 8.10 s with 1182 and 585 (through `cellrt smoke-llm`).
Nearly 600 completion tokens for one short tool call suggests the model reasons before it
answers; reasoning was left at the host's default.

So tool calling works, and the planner stays in tool mode. Not tried yet: `"required"`, JSON
mode, reasoning switched off. One call says the path works, not how often the model is right:
that is the eval's job (#52).
