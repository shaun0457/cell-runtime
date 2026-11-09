"""CLI seam for the model planner: `cellrt eval | run --planner llm` and `cellrt smoke-llm`, in
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

from cellrt.planner.nim import KEY_ENV, MISSING_KEY, SDK_ENV_VARS

REPO = Path(__file__).resolve().parent.parent
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"
LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"

# Answers every call with a switch to insert_act, and logs the model asked to FAKE_OPENAI_LOG.
FAKE_OPENAI = '''
"""A stand-in for the openai SDK, for the CLI tests."""
import os
from types import SimpleNamespace

ANSWER = '{"kind": "switch_skill", "skill": "insert_act"}'


class AsyncOpenAI:
    def __init__(self, **settings):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def _create(self, **arguments):
        with open(os.environ["FAKE_OPENAI_LOG"], "a", encoding="utf-8") as log:
            log.write(arguments["model"] + "\\n")
        call = SimpleNamespace(
            id="call_0", type="function", function=SimpleNamespace(name="decide", arguments=ANSWER)
        )
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
    assert f"planner for setting C: llm ({LIGHTNING})" in done.stderr
    assert f"model calls: {len(calls)} of 6" in done.stderr
    assert FAKE_KEY.encode() not in cli.db.read_bytes()  # nor in the event log


def test_the_model_is_chosen_on_the_command_line(cli: Cli) -> None:
    done = _eval(cli.with_key(), "--model", "nvidia/other-model")

    assert done.returncode == 0, done.stderr
    assert set(cli.calls()) == {"nvidia/other-model"}


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


def test_smoke_llm_without_a_key_says_where_the_key_goes(cli: Cli) -> None:
    done = cli("smoke-llm")

    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == MISSING_KEY + "\n" and KEY_ENV in done.stderr


def test_smoke_llm_refuses_an_unknown_mode_with_a_fixed_message(cli: Cli) -> None:
    done = cli.with_key()("smoke-llm", "--mode", "xml")

    assert done.returncode == 1
    assert done.stderr == "mode must be one of tool, json\n"
    assert cli.calls() == []
