"""`cellrt promote propose` in a subprocess, the way a person runs it. A stand-in `openai`
package on PYTHONPATH answers the one call from FAKE_ANSWER and logs it, so no test needs the
SDK, a key or the network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from nim_fakes import FAKE_KEY

from cellrt.eval.runner import run_eval
from cellrt.eval.scenarios import load_scenarios
from cellrt.planner.nim import KEY_ENV, MISSING_KEY, SDK_ENV_VARS
from cellrt.runtime.store import EventStore

REPO = Path(__file__).resolve().parents[1]
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"
ROTATED = "insert_scripted/insert_stalled force <10 N torque >=0.1 Nm"
CANDIDATE = {
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

FAKE_OPENAI = '''
"""A stand-in for the openai SDK: answers with the `propose` tool and FAKE_ANSWER. With
FAKE_ECHO set it hands back the key it was given: in the answer, or in an error's text."""
import json
import os
from types import SimpleNamespace


class AsyncOpenAI:
    def __init__(self, **settings):
        self._key = settings["api_key"]
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def _create(self, **arguments):
        with open(os.environ["FAKE_OPENAI_LOG"], "a", encoding="utf-8") as log:
            log.write(arguments["tools"][0]["function"]["name"] + "\\n")
        echo = os.environ.get("FAKE_ECHO")
        if echo == "raise":
            raise RuntimeError("401: invalid key " + self._key)
        answer = os.environ["FAKE_ANSWER"]
        if echo == "answer":
            answer = json.dumps({"rule": {"name": self._key}, "plan": {"action": "abort"}})
        function = SimpleNamespace(name="propose", arguments=answer)
        call = SimpleNamespace(id="call_0", type="function", function=function)
        message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        choice = SimpleNamespace(index=0, message=message, finish_reason="tool_calls")
        usage = SimpleNamespace(prompt_tokens=1500, completion_tokens=300)
        return SimpleNamespace(model=arguments["model"], choices=[choice], usage=usage)
'''


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("propose-cli") / "phase3.sqlite"
    with EventStore(path) as s:
        run_eval(load_scenarios(PHASE3), ["A", "B", "C"], 3, s)
    return path


class Cli:
    def __init__(self, tmp_path: Path, answer: str = json.dumps(CANDIDATE)) -> None:
        sdk = tmp_path / "sdk" / "openai"
        sdk.mkdir(parents=True)
        (sdk / "__init__.py").write_text(FAKE_OPENAI, encoding="utf-8")
        self.log = tmp_path / "calls.log"
        self.env = {k: v for k, v in os.environ.items() if k != KEY_ENV and k not in SDK_ENV_VARS}
        self.env.update(
            PYTHONPATH=str(sdk.parent), FAKE_OPENAI_LOG=str(self.log), FAKE_ANSWER=answer
        )

    def with_key(self) -> Cli:
        self.env[KEY_ENV] = FAKE_KEY
        return self

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            [sys.executable, "-m", "cellrt", *args],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            cwd=REPO,
            env=self.env,
        )
        assert FAKE_KEY not in done.stdout + done.stderr
        return done

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


def _propose(
    cli: Cli, store: Path, tmp_path: Path, group: str = ROTATED
) -> subprocess.CompletedProcess[str]:
    return cli(
        "promote", "propose", "--group", group, "--db", str(store),
        "--out", str(tmp_path / "proposal.json"),
        "--candidate-out", str(tmp_path / "candidate.json"),
    )  # fmt: skip


def test_a_valid_proposal_writes_a_candidate_that_validate_passes(
    store: Path, tmp_path: Path
) -> None:
    cli = Cli(tmp_path).with_key()
    done = _propose(cli, store, tmp_path)
    assert done.returncode == 0, done.stderr
    assert cli.calls() == ["propose"]
    assert done.stdout.splitlines()[0] == "outcome: candidate"
    record = (tmp_path / "proposal.json").read_text(encoding="utf-8")
    assert FAKE_KEY not in record
    data = json.loads(record)
    assert data["outcome"] == "candidate"
    # The prompt sent and the raw answer are both kept.
    assert ROTATED in data["prompt"] and "insert_act" in data["prompt"]
    assert data["response"] == json.dumps(CANDIDATE)
    assert (data["prompt_tokens"], data["completion_tokens"]) == (1500, 300)
    checked = cli(
        "promote", "validate", str(tmp_path / "candidate.json"), "--group", ROTATED,
        "--db", str(store),
    )  # fmt: skip
    assert checked.returncode == 0, checked.stdout


def test_a_rejected_answer_is_recorded_and_leaves_no_candidate(store: Path, tmp_path: Path) -> None:
    # A candidate from an earlier run is removed: validate must not check it in this one's name.
    (tmp_path / "candidate.json").write_text(json.dumps(CANDIDATE), encoding="utf-8")
    cli = Cli(tmp_path, answer='{"rule": {"name": "x"}}').with_key()
    done = _propose(cli, store, tmp_path)
    assert done.returncode == 1
    assert done.stdout.splitlines()[0] == "outcome: rejected"
    assert json.loads((tmp_path / "proposal.json").read_text(encoding="utf-8"))["errors"]
    assert not (tmp_path / "candidate.json").exists()


@pytest.mark.parametrize("echo, outcome", [("answer", "rejected"), ("raise", "error")])
def test_a_key_the_provider_hands_back_is_never_printed_or_kept(
    store: Path, tmp_path: Path, echo: str, outcome: str
) -> None:
    cli = Cli(tmp_path).with_key()
    cli.env["FAKE_ECHO"] = echo
    done = _propose(cli, store, tmp_path)  # Cli checks stdout and stderr
    assert done.stdout.splitlines()[0] == f"outcome: {outcome}"
    assert FAKE_KEY not in (tmp_path / "proposal.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("clash", ["out_on_store", "candidate_on_store", "candidate_on_out"])
def test_an_output_on_the_store_or_on_the_other_output_is_refused_before_the_call(
    store: Path, tmp_path: Path, clash: str
) -> None:
    before = store.read_bytes()
    out, candidate = str(tmp_path / "proposal.json"), str(tmp_path / "candidate.json")
    if clash == "out_on_store":
        out = str(store)
    elif clash == "candidate_on_store":
        candidate = str(store)
    else:
        candidate = out
    cli = Cli(tmp_path).with_key()
    done = cli(
        "promote", "propose", "--group", ROTATED, "--db", str(store),
        "--out", out, "--candidate-out", candidate,
    )  # fmt: skip
    assert done.returncode == 1
    assert "nothing sent" in done.stderr
    assert cli.calls() == []
    assert store.read_bytes() == before


def test_without_a_key_nothing_is_read_or_sent(store: Path, tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    done = _propose(cli, store, tmp_path)
    assert done.returncode == 1
    assert done.stderr == MISSING_KEY + "\n"
    assert cli.calls() == [] and not (tmp_path / "proposal.json").exists()


def test_a_group_the_store_does_not_have_is_refused_before_the_call(
    store: Path, tmp_path: Path
) -> None:
    cli = Cli(tmp_path).with_key()
    done = _propose(cli, store, tmp_path, group="verify/none")
    assert done.returncode == 1
    assert "no UNKNOWN group with that label" in done.stderr
    assert cli.calls() == []
