"""CLI seam for the model planners: `cellrt eval | run --planner llm | --tree llm` and
`cellrt smoke-llm`, in
a subprocess the way a person runs them. A stand-in `openai` package on PYTHONPATH answers every
call and logs it, so no test needs the SDK, a key or the network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from nim_fakes import FAKE_KEY

from cellrt.planner.llm import MAX_TOKENS_LIMIT
from cellrt.planner.nim import KEY_ENV, MISSING_KEY, SDK_ENV_VARS

REPO = Path(__file__).resolve().parent.parent
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"
TASKS = REPO / "eval" / "scenarios" / "phase3-tasks.yaml"
LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"
THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}

# Answers through the tool it is offered: a switch to insert_act for `decide` (an UNKNOWN), a
# plan that inserts with insert_act for `plan` (a task), or an empty plan when the model is
# `fake/rejects`. Logs the model asked to FAKE_OPENAI_LOG, and the token budget and the extra body
# sent beside it.
FAKE_OPENAI = '''
"""A stand-in for the openai SDK, for the CLI tests."""
import json
import os
from types import SimpleNamespace

ANSWERS = {
    "decide": '{"kind": "switch_skill", "skill": "insert_act"}',
    "plan": '{"steps": ["approach", "align", "insert_act", "verify"]}',
}


class OpenAI:
    def __init__(self, **settings):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def _create(self, **arguments):
        with open(os.environ["FAKE_OPENAI_LOG"], "a", encoding="utf-8") as log:
            log.write(arguments["model"] + "\\n")
        sent = {"max_tokens": arguments["max_tokens"], "extra_body": arguments.get("extra_body")}
        with open(os.environ["FAKE_OPENAI_LOG"] + ".args", "a", encoding="utf-8") as log:
            log.write(json.dumps(sent) + "\\n")
        tools = arguments.get("tools") or [{"function": {"name": "decide"}}]
        name = tools[0]["function"]["name"]
        answer = ANSWERS[name]
        if name == "plan" and arguments["model"] == "fake/rejects":
            answer = '{"steps": []}'
        function = SimpleNamespace(name=name, arguments=answer)
        call = SimpleNamespace(id="call_0", type="function", function=function)
        message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        choice = SimpleNamespace(index=0, message=message, finish_reason="tool_calls")
        usage = SimpleNamespace(prompt_tokens=812, completion_tokens=37)
        return SimpleNamespace(model=arguments["model"], choices=[choice], usage=usage)
'''


class Cli:
    """`cellrt` with the stand-in SDK, and the key set or not."""

    def __init__(self, tmp_path: Path) -> None:
        sdk = tmp_path / "sdk" / "openai"
        sdk.mkdir(parents=True)
        (sdk / "__init__.py").write_text(FAKE_OPENAI, encoding="utf-8")
        self.log = tmp_path / "calls.log"
        self.db = tmp_path / "cli.sqlite"
        self.env = {k: v for k, v in os.environ.items() if k != KEY_ENV and k not in SDK_ENV_VARS}
        self.env.update(PYTHONPATH=str(sdk.parent), FAKE_OPENAI_LOG=str(self.log))

    def with_key(self) -> Cli:
        self.env[KEY_ENV] = FAKE_KEY
        return self

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            [sys.executable, "-m", "cellrt", *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=REPO,
            env=self.env,
        )
        assert FAKE_KEY not in done.stdout + done.stderr  # never, whatever happened
        return done

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def sent(self) -> list[dict[str, object]]:
        """Each call's `max_tokens` and `extra_body`, in order."""
        log = self.log.with_name(self.log.name + ".args")
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [json.loads(line) for line in lines]


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    return Cli(tmp_path)


def _eval(cli: Cli, *extra: str) -> subprocess.CompletedProcess[str]:
    return cli("eval", "--scenarios", str(PHASE3), "--settings", "C", "--seeds", "1",
               "--k", "1", "--db", str(cli.db), "--planner", "llm", *extra)  # fmt: skip


def test_an_eval_with_the_model_planner_and_no_key_fails_before_any_run(cli: Cli) -> None:
    done = _eval(cli)

    assert done.returncode == 1
    assert done.stderr == MISSING_KEY + "\n"
    assert not cli.db.exists() and cli.calls() == []


def test_an_eval_that_could_exceed_the_call_cap_does_not_start(cli: Cli) -> None:
    # 3 scenarios x 30 seeds in setting C, 2 planner calls each: up to 180 calls.
    done = _eval(cli.with_key(), "--seeds", "30", "--max-model-calls", "179")

    assert done.returncode == 1
    assert "up to 180 model calls" in done.stderr and "cap of 179" in done.stderr
    assert not cli.db.exists() and cli.calls() == []


def test_an_eval_with_the_model_planner_counts_its_calls(cli: Cli) -> None:
    done = _eval(cli.with_key(), "--max-model-calls", "6")

    assert done.returncode == 0, done.stderr
    calls = cli.calls()
    assert calls and set(calls) == {LIGHTNING}
    assert (
        f"planner for setting C: llm ({LIGHTNING}, 1024 tokens, reasoning default)" in done.stderr
    )
    assert f"model calls: {len(calls)} of 6" in done.stderr
    assert FAKE_KEY.encode() not in cli.db.read_bytes()  # nor in the event log
    # By default: 1024 tokens a call, reasoning as the host sets it.
    assert cli.sent() == [{"max_tokens": 1024, "extra_body": None}] * len(calls)


def test_the_model_is_chosen_on_the_command_line(cli: Cli) -> None:
    done = _eval(cli.with_key(), "--model", "nvidia/other-model")

    assert done.returncode == 0, done.stderr
    assert set(cli.calls()) == {"nvidia/other-model"}


def test_the_token_budget_and_reasoning_are_chosen_on_the_command_line(cli: Cli) -> None:
    done = _eval(cli.with_key(), "--max-tokens", "4096", "--reasoning", "off")

    assert done.returncode == 0, done.stderr
    sent = cli.sent()
    assert sent and sent == [{"max_tokens": 4096, "extra_body": THINKING_OFF}] * len(sent)
    # Two evals that differ only in these settings say so: the table does not.
    assert f"llm ({LIGHTNING}, 4096 tokens, reasoning off)" in done.stderr


@pytest.mark.parametrize("tokens", ["0", str(MAX_TOKENS_LIMIT + 1)])
def test_a_token_budget_out_of_range_is_refused_before_any_run(cli: Cli, tokens: str) -> None:
    done = _eval(cli.with_key(), "--max-tokens", tokens)

    assert done.returncode == 2  # a usage error
    assert not cli.db.exists() and cli.calls() == []


def test_an_unknown_reasoning_setting_is_refused_before_any_run(cli: Cli) -> None:
    done = _eval(cli.with_key(), "--reasoning", "on")

    assert done.returncode == 1
    assert done.stderr == "unknown reasoning 'on'; known: default, off\n"
    assert not cli.db.exists() and cli.calls() == []


def test_a_run_without_setting_c_needs_no_key(cli: Cli) -> None:
    done = cli("run", "--scenarios", str(PHASE3), "--scenario", "yaw_mismatch",
               "--setting", "B", "--db", str(cli.db), "--planner", "llm")  # fmt: skip

    assert done.returncode == 0, done.stderr
    assert cli.calls() == [] and "model calls" not in done.stderr


def test_a_run_in_setting_c_asks_the_model(cli: Cli) -> None:
    done = cli.with_key()("run", "--scenarios", str(PHASE3), "--scenario", "yaw_mismatch",
                          "--setting", "C", "--db", str(cli.db), "--planner", "llm")  # fmt: skip

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "outcome: success"
    assert cli.calls() == [LIGHTNING]
    assert "model calls: 1 of 100" in done.stderr


def _run_tasks(cli: Cli, *extra: str) -> subprocess.CompletedProcess[str]:
    return cli("run", "--scenarios", str(TASKS), "--scenario", "sc", "--db", str(cli.db),
               "--tree", "llm", *extra)  # fmt: skip


def test_a_run_with_the_model_task_planner_asks_the_model_once_before_it_moves(
    cli: Cli,
) -> None:
    done = _run_tasks(cli.with_key(), "--setting", "B")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "outcome: success"  # it planned insert_act
    assert cli.calls() == [LIGHTNING]
    assert "model calls: 1 of 100" in done.stderr
    assert FAKE_KEY.encode() not in cli.db.read_bytes()


def test_the_task_planner_takes_the_same_token_budget_and_reasoning(cli: Cli) -> None:
    done = _run_tasks(cli.with_key(), "--setting", "B", "--max-tokens", str(MAX_TOKENS_LIMIT),
                      "--reasoning", "off")  # fmt: skip

    assert done.returncode == 0, done.stderr
    assert cli.sent() == [{"max_tokens": MAX_TOKENS_LIMIT, "extra_body": THINKING_OFF}]


def test_the_model_task_planner_without_a_key_fails_before_any_run(cli: Cli) -> None:
    done = _run_tasks(cli, "--setting", "A")

    assert done.returncode == 1
    assert done.stderr == MISSING_KEY + "\n"
    assert not cli.db.exists() and cli.calls() == []


def test_the_call_pre_check_counts_one_plan_call_per_task_and_seed(cli: Cli) -> None:
    # 2 tasks x 1 seed to plan, shared by the 3 settings, plus 2 units of C x 2 planner calls:
    # up to 6 calls.
    done = cli.with_key()("eval", "--scenarios", str(TASKS), "--seeds", "1", "--k", "1",
                          "--db", str(cli.db), "--tree", "llm", "--planner", "llm",
                          "--max-model-calls", "5")  # fmt: skip

    assert done.returncode == 1
    assert "up to 6 model calls" in done.stderr and "plus 2 task plan calls" in done.stderr
    assert not cli.db.exists() and cli.calls() == []


def test_an_eval_with_the_model_task_planner_counts_every_call_under_one_cap(cli: Cli) -> None:
    done = cli.with_key()("eval", "--scenarios", str(TASKS), "--seeds", "1", "--k", "1",
                          "--db", str(cli.db), "--tree", "llm", "--planner", "llm",
                          "--max-model-calls", "10")  # fmt: skip

    assert done.returncode == 0, done.stderr
    calls = cli.calls()
    assert len(calls) == 2  # one plan per task, shared by its settings; no UNKNOWN triage
    assert f"tree: llm ({LIGHTNING}, 1024 tokens, reasoning default)" in done.stderr
    assert "model calls: 2 of 10" in done.stderr
    assert "task plans rejected: 0 of 6 runs" in done.stderr


def test_an_eval_says_how_many_task_plans_were_rejected(cli: Cli) -> None:
    done = cli.with_key()("eval", "--scenarios", str(TASKS), "--settings", "A", "--seeds", "1",
                          "--k", "1", "--db", str(cli.db), "--tree", "llm",
                          "--model", "fake/rejects")  # fmt: skip

    assert done.returncode == 0, done.stderr
    # The table counts them as aborted tasks with no steps; stderr says why.
    assert "task plans rejected: 2 of 2 runs" in done.stderr


def test_smoke_llm_prints_one_answer_as_json(cli: Cli) -> None:
    done = cli.with_key()("smoke-llm")

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert (report["answer"], report["valid"], report["decision"]) == (
        "tool_call",
        True,
        "switch_skill",
    )
    assert (report["model"], report["prompt_tokens"]) == (LIGHTNING, 812)
    assert cli.calls() == [LIGHTNING]  # one call, no more


def test_smoke_llm_takes_the_token_budget_and_reasoning(cli: Cli) -> None:
    done = cli.with_key()("smoke-llm", "--max-tokens", "1", "--reasoning", "off")

    assert done.returncode == 0, done.stderr
    assert cli.sent() == [{"max_tokens": 1, "extra_body": THINKING_OFF}]
    report = json.loads(done.stdout)
    assert (report["max_tokens"], report["reasoning"]) == (1, "off")


def test_smoke_llm_refuses_an_unknown_reasoning_setting(cli: Cli) -> None:
    done = cli.with_key()("smoke-llm", "--reasoning", "none")

    assert done.returncode == 1
    assert done.stderr == "unknown reasoning 'none'; known: default, off\n"
    assert cli.calls() == []


def test_smoke_llm_without_a_key_says_where_the_key_goes(cli: Cli) -> None:
    done = cli("smoke-llm")

    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == MISSING_KEY + "\n" and KEY_ENV in done.stderr


def test_smoke_llm_refuses_an_unknown_mode_with_a_fixed_message(cli: Cli) -> None:
    done = cli.with_key()("smoke-llm", "--mode", "xml")

    assert done.returncode == 1
    assert done.stderr == "mode must be one of tool, json\n"
    assert cli.calls() == []
