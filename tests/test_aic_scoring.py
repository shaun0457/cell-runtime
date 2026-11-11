"""The scorer's files as the referee of a trial (`adapters/aic/scoring.py`, ADR 029)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from adapters.aic.scoring import NOT_A_DUMP, ScoringInputError, referee_trial, sim_time

TARGET = ("nic_card_mount_0", "sfp_port_0")
S = 1_000_000_000  # ns
SCORING = Path(__file__).parent / "fixtures" / "sim" / "scoring_f1clean.yaml"


def _pairs(start_recv: int = 10 * S, start_sim: int = 100 * S, n: int = 100) -> list[list[int]]:
    """Wrench pairs at 50 Hz of sim time, received at a real-time factor of 0.5."""
    return [[start_recv + i * 40_000_000, start_sim + i * 20_000_000] for i in range(n)]


def _dump(*insertions: tuple[int, str], pairs: list[list[int]] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "insertions": [{"recv_ns": recv, "port": port} for recv, port in insertions],
        "contacts": [{"recv_ns": 11 * S, "count": 2}],
        "pairs": _pairs() if pairs is None else pairs,
    }


def test_a_receive_time_is_interpolated_between_its_two_pairs() -> None:
    mapped = sim_time(
        10 * S + 20_000_000, [(10 * S, 100 * S), (10 * S + 40_000_000, 100 * S + 20_000_000)]
    )

    assert mapped is not None
    sim_s, tau = mapped
    assert sim_s == pytest.approx(100.01)
    assert tau == 0.25  # 3 x 20 ms is under the floor


def test_a_wide_gap_widens_the_tolerance() -> None:
    mapped = sim_time(10 * S + S // 2, [(10 * S, 100 * S), (11 * S, 100 * S + 200_000_000)])

    assert mapped is not None
    assert mapped[1] == pytest.approx(0.6)  # 3 x 0.2 s


def test_a_receive_time_on_a_pair_takes_its_stamp() -> None:
    assert sim_time(10 * S, [(10 * S, 100 * S), (11 * S, 101 * S)]) == (100.0, 0.25)


def test_pairs_received_at_the_events_time_give_the_earliest_stamp_and_their_spread() -> None:
    pairs = [(9 * S, 99 * S), (10 * S, 100 * S + 200_000_000), (10 * S, 100 * S), (11 * S, 101 * S)]

    assert sim_time(10 * S, pairs) == (100.0, pytest.approx(0.6))  # 3 x the 0.2 s between them
    assert sim_time(10 * S, pairs[1:3]) == (100.0, pytest.approx(0.6))  # no pair on either side


@pytest.mark.parametrize(
    "recv",
    [9 * S, 12 * S],  # before the first pair, after the last
)
def test_a_time_outside_the_pairs_is_not_mapped(recv: int) -> None:
    assert sim_time(recv, [(10 * S, 100 * S), (11 * S, 101 * S)]) is None


def test_pairs_up_to_1_s_away_map_and_further_ones_do_not() -> None:
    pairs = [(10 * S, 100 * S), (12 * S, 101 * S)]

    assert sim_time(11 * S, pairs) is not None  # exactly 1 s on each side
    assert sim_time(11 * S + 1, pairs) is None
    assert sim_time(11 * S - 1, pairs) is None


def test_stamps_that_run_backwards_are_not_mapped() -> None:
    assert sim_time(10 * S + S // 2, [(10 * S, 101 * S), (11 * S, 100 * S)]) is None


def test_no_pairs_is_no_mapping() -> None:
    assert sim_time(10 * S, []) is None


@pytest.mark.parametrize("port", ["nic_card_mount_0/sfp_port_0", "/nic_card_mount_0/sfp_port_0"])
def test_an_insertion_into_the_target_is_read_as_the_scorer_reads_it(port: str) -> None:
    referee = referee_trial(None, _dump((11 * S, port)), TARGET)

    assert referee.inserted is True and referee.wrong_port is False
    assert referee.insertion_sim_s == pytest.approx(100.5)  # 1 s of wall at 0.5


@pytest.mark.parametrize("port", ["nic_card_mount_1/sfp_port_0", "/nic_card_mount_0/sfp_port_1"])
def test_an_insertion_into_another_port_is_named_and_has_no_time(port: str) -> None:
    referee = referee_trial(None, _dump((11 * S, port)), TARGET)

    assert referee.inserted is False and referee.wrong_port is True
    assert referee.insertion_sim_s is None


def test_the_last_event_decides_and_the_first_into_the_target_gives_the_time() -> None:
    dump = _dump(
        (11 * S, "/nic_card_mount_0/sfp_port_0"),
        (12 * S, "/nic_card_mount_1/sfp_port_0"),
        (13 * S, "/nic_card_mount_0/sfp_port_0"),
    )

    referee = referee_trial(None, dump, TARGET)

    assert referee.inserted is True
    assert referee.insertion_sim_s == pytest.approx(100.5)  # the event at 11 s


def test_a_trial_whose_last_event_is_another_port_is_not_inserted() -> None:
    dump = _dump((11 * S, "nic_card_mount_0/sfp_port_0"), (12 * S, "nic_card_mount_1/sfp_port_0"))

    assert referee_trial(None, dump, TARGET).inserted is False


def test_no_insertion_event_is_no_insertion() -> None:
    referee = referee_trial(None, _dump(), TARGET)

    assert referee.last_port is None
    assert referee.inserted is False and referee.wrong_port is False


def test_an_insertion_the_pairs_cannot_place_has_no_time() -> None:
    referee = referee_trial(None, _dump((30 * S, "nic_card_mount_0/sfp_port_0")), TARGET)

    assert referee.inserted is True
    assert referee.insertion_sim_s is None and referee.tolerance_s is None


def test_the_scores_come_from_the_trials_entry() -> None:
    scores = yaml.safe_load(SCORING.read_text("utf-8"))

    referee = referee_trial(scores["trial_1"], _dump(), TARGET)

    assert referee.tier3 == 75.0
    assert referee.force_penalty == 0.0 and referee.contacts_score == 0.0
    assert referee.total == pytest.approx(1 + 17.859139818693926 + 75)
    assert referee.contact_messages == 1


@pytest.mark.parametrize(
    "score",
    [
        None,
        {},
        {"tier_2": "x"},
        {"tier_3": {"score": "75"}},
        {"tier_3": {"score": float("inf")}},
        {"tier_3": {"score": 10**400}},  # too large for a float: OverflowError, not a crash
    ],
    ids=["none", "empty", "not a map", "a string", "infinite", "overflow"],
)
def test_a_missing_or_odd_score_is_none(score: object) -> None:
    referee = referee_trial(score, _dump(), TARGET)

    assert referee.tier3 is None and referee.total is None and referee.force_penalty is None


@pytest.mark.parametrize(
    "dump",
    [
        None,
        [],
        {"version": 2, "insertions": [], "contacts": [], "pairs": []},
        {"version": 1, "insertions": [], "contacts": []},
        {"version": 1, "insertions": [{"recv_ns": "1", "port": "p"}], "contacts": [], "pairs": []},
        {"version": 1, "insertions": [{"recv_ns": True, "port": "p"}], "contacts": [], "pairs": []},
        {"version": 1, "insertions": [{"recv_ns": 1, "port": 3}], "contacts": [], "pairs": []},
        {"version": 1, "insertions": [], "contacts": [{"count": 1}], "pairs": []},
        {"version": 1, "insertions": [], "contacts": [], "pairs": [[1, 2, 3]]},
        {"version": 1, "insertions": [], "contacts": [], "pairs": [[1, 2.5]]},
        {"version": 1, "insertions": "x", "contacts": [], "pairs": []},
        # Beyond int64 (a float overflow in the mapping), or negative: not a bag's time.
        {"version": 1, "insertions": [], "contacts": [], "pairs": [[1 << 63, 2]]},
        {"version": 1, "insertions": [], "contacts": [], "pairs": [[1, 10**400]]},
        {"version": 1, "insertions": [{"recv_ns": -1, "port": "p"}], "contacts": [], "pairs": []},
        {"version": 1, "insertions": [], "contacts": [{"recv_ns": 1, "count": -2}], "pairs": []},
    ],
)
def test_a_malformed_dump_is_refused_with_a_fixed_message(dump: object) -> None:
    with pytest.raises(ScoringInputError) as refused:
        referee_trial(None, dump, TARGET)

    assert str(refused.value) == NOT_A_DUMP
