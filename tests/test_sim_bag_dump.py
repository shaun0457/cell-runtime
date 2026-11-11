"""The bag reader that runs inside the eval container (`tools/sim/in_container/bag_dump.py`,
ADR 029). Its pure part, which turns (topic, receive time, value) records into the referee's
JSON with a bound on every list, runs anywhere; reading a real bag needs rosbag2_py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.sim.in_container.bag_dump as bag_dump
from tools.sim.in_container.bag_dump import (
    CONTACTS_OVER,
    CONTACTS_TOPIC,
    INSERTION_TOPIC,
    INSERTIONS_OVER,
    MAX_CONTACT_MSGS,
    MAX_INSERTIONS,
    MAX_PAIRS,
    MAX_PORT_CHARS,
    PAIRS_OVER,
    PORT_TOO_LONG,
    WRENCH_TOPIC,
    BagRefused,
    collect,
    main,
)

PORT = "/nic_card_mount_0/sfp_port_0"  # the touch topic's namespace, kept as sent


def test_records_become_insertions_contacts_and_time_pairs() -> None:
    records = [
        (WRENCH_TOPIC, 1_000, 40_000_000_000),
        ("/joint_states", 1_500, None),  # not the referee's: ignored
        (INSERTION_TOPIC, 2_000, PORT),
        (CONTACTS_TOPIC, 2_100, 0),  # a message with no contact is not kept
        (CONTACTS_TOPIC, 2_200, 3),
        (WRENCH_TOPIC, 3_000, 40_020_000_000),
    ]

    assert collect(records) == {
        "version": 1,
        "insertions": [{"recv_ns": 2_000, "port": PORT}],
        "contacts": [{"recv_ns": 2_200, "count": 3}],
        "pairs": [[1_000, 40_000_000_000], [3_000, 40_020_000_000]],
    }


@pytest.mark.parametrize(
    ("topic", "value", "bound", "message"),
    [
        (WRENCH_TOPIC, 1, MAX_PAIRS, PAIRS_OVER),
        (INSERTION_TOPIC, PORT, MAX_INSERTIONS, INSERTIONS_OVER),
        (CONTACTS_TOPIC, 1, MAX_CONTACT_MSGS, CONTACTS_OVER),
    ],
)
def test_each_list_is_kept_up_to_its_bound_and_refused_past_it(
    topic: str, value: object, bound: int, message: str
) -> None:
    at_bound = [(topic, i, value) for i in range(bound)]
    assert len(next(v for k, v in collect(at_bound).items() if isinstance(v, list) and v)) == bound

    with pytest.raises(BagRefused) as refused:
        collect([*at_bound, (topic, bound, value)])
    assert str(refused.value) == message


def test_a_port_name_longer_than_its_bound_is_refused_without_repeating_it() -> None:
    long = "x" * (MAX_PORT_CHARS + 1)
    assert collect([(INSERTION_TOPIC, 1, "x" * MAX_PORT_CHARS)])["insertions"]

    with pytest.raises(BagRefused) as refused:
        collect([(INSERTION_TOPIC, 1, long)])
    assert str(refused.value) == PORT_TOO_LONG


def test_main_needs_two_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_writes_the_summary_in_one_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bag_dump, "_records", lambda bag: iter([(INSERTION_TOPIC, 7, PORT)]))
    out = tmp_path / "trial_1.json"

    assert main([str(tmp_path / "bag"), str(out)]) == 0

    assert json.loads(out.read_text("utf-8"))["insertions"] == [{"recv_ns": 7, "port": PORT}]
    assert sorted(f.name for f in tmp_path.iterdir()) == ["trial_1.json"]  # no partial file


def test_a_refused_bag_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long = "x" * (MAX_PORT_CHARS + 1)
    monkeypatch.setattr(bag_dump, "_records", lambda bag: iter([(INSERTION_TOPIC, 7, long)]))

    assert main([str(tmp_path / "bag"), str(tmp_path / "trial_1.json")]) == 1

    assert list(tmp_path.iterdir()) == []


def test_main_reads_a_real_bag(tmp_path: Path) -> None:
    pytest.importorskip("rosbag2_py")  # only inside the eval container
    out = tmp_path / "trial.json"

    assert main([str(tmp_path / "missing_bag"), str(out)]) == 1
    assert not out.exists() or json.loads(out.read_text("utf-8"))
