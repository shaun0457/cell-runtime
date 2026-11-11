"""Wilson intervals, the exact sign test and ADR 030's pooled claim (`cellrt/eval/intervals.py`)."""

from __future__ import annotations

import pytest

from cellrt.eval.intervals import (
    NO_CLAIM,
    SceneTally,
    claim,
    format_interval,
    overlap,
    sign_test,
    wilson,
)


# Newcombe (1998), "Two-sided confidence intervals for the single proportion", Stat Med 17:857,
# Table I, method 3 (score interval without continuity correction).
@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0, 0.1611),
        (1, 29, 0.0061, 0.1718),
        (29, 29, 0.8830, 1.0),
    ],
)
def test_wilson_matches_the_published_values(
    successes: int, n: int, low: float, high: float
) -> None:
    interval = wilson(successes, n)

    assert interval is not None
    assert interval[0] == pytest.approx(low, abs=1e-4)
    assert interval[1] == pytest.approx(high, abs=1e-4)


def test_a_rate_of_no_trials_is_undefined_and_shown_as_a_dash() -> None:
    assert wilson(0, 0) is None
    assert format_interval(wilson(0, 0)) == "-"
    assert format_interval((0.1234, 0.5)) == "[0.12, 0.50]"


@pytest.mark.parametrize(("successes", "n"), [(-1, 3), (4, 3), (0, -1)])
def test_wilson_refuses_counts_that_are_not_a_rate(successes: int, n: int) -> None:
    with pytest.raises(ValueError):
        wilson(successes, n)


@pytest.mark.parametrize(
    ("wins", "losses", "p"),
    [
        (9, 1, 22 / 1024),  # 2 * (C(10,0) + C(10,1)) / 2^10
        (1, 9, 22 / 1024),
        (6, 0, 2 / 64),  # six scenes, all one way: the fewest that reach p < 0.05
        (5, 0, 2 / 32),  # five do not
        (3, 3, 1.0),  # the doubled tail passes 1 and is capped
        (0, 0, 1.0),  # every scene tied: nothing to test
    ],
)
def test_the_sign_test_gives_the_exact_binomial_p(wins: int, losses: int, p: float) -> None:
    assert sign_test(wins, losses) == pytest.approx(p, abs=1e-12)


def test_the_sign_test_refuses_a_negative_count() -> None:
    with pytest.raises(ValueError):
        sign_test(-1, 2)


def test_intervals_overlap_when_they_share_a_point() -> None:
    assert overlap((0.1, 0.4), (0.4, 0.9))
    assert not overlap((0.1, 0.39), (0.4, 0.9))
    assert overlap(None, (0.4, 0.9))


def _scenes(b_wins: int, a_wins: int, ties: int) -> list[SceneTally]:
    return [
        *(SceneTally(f"b{i}", 0, 3, 3, 3) for i in range(b_wins)),
        *(SceneTally(f"a{i}", 3, 3, 0, 3) for i in range(a_wins)),
        *(SceneTally(f"t{i}", 2, 3, 2, 3) for i in range(ties)),
    ]


def test_a_scene_with_a_repeat_lost_is_won_by_the_higher_rate() -> None:
    # A repeat lost to infrastructure is left out: 2 of 2 beats 2 of 3, though the counts tie.
    assert SceneTally("s", 2, 2, 2, 3).a_better
    assert not SceneTally("s", 2, 2, 2, 3).b_better
    assert not SceneTally("s", 1, 2, 1, 2).a_better  # an exact tie is dropped
    assert SceneTally("s", 0, 2, 1, 3).b_better


@pytest.mark.parametrize("counts", [(1, 0, 0, 1), (0, 1, 2, 1), (0, 0, 0, 1), (-1, 1, 0, 1)])
def test_a_scene_tally_must_be_counts_out_of_trials(counts: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        SceneTally("s", *counts)


def test_b_beats_a_only_when_the_intervals_part_and_the_scenes_agree() -> None:
    result = claim(10, 72, 50, 72, _scenes(8, 0, 16))

    assert result.verdict == "B beats A"
    assert (result.b_wins, result.a_wins, result.ties) == (8, 0, 16)
    assert result.p == pytest.approx(2 / 256)


def test_a_beats_b_by_the_same_rule_the_other_way_round() -> None:
    assert claim(50, 72, 10, 72, _scenes(0, 8, 16)).verdict == "A beats B"


def test_parted_intervals_alone_are_no_claim() -> None:
    # Trial-level counts can part while only 5 scenes differ: their 3 repeats are not
    # independent evidence, so the sign test (p = 0.0625) holds the claim back.
    result = claim(0, 15, 15, 15, _scenes(5, 0, 0))

    assert result.a is not None and result.b is not None and result.b[0] > result.a[1]
    assert result.verdict == NO_CLAIM


def test_a_sign_test_alone_is_no_claim() -> None:
    result = claim(30, 72, 40, 72, _scenes(8, 0, 16))

    assert result.p < 0.05
    assert result.verdict == NO_CLAIM


def test_the_two_tests_must_point_the_same_way() -> None:
    assert claim(10, 72, 50, 72, _scenes(0, 8, 16)).verdict == NO_CLAIM


def test_no_trials_is_no_claim() -> None:
    assert claim(0, 0, 0, 0, []).verdict == NO_CLAIM
