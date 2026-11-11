"""The scorer's files as the referee of one trial (ADR 029). ROS-free and pure.

Inputs: the trial's entry in `scoring.yaml`, and the JSON `tools/sim/in_container/bag_dump.py`
writes from the trial's bag (insertion events, off-limit contact messages, and wrench time
pairs).

**Which insertion.** The scorer scores its last insertion event (`ScoringTier2.cc:444-447`),
split on `/` with empty parts dropped (`:812-837`). When that event is into the target port, the
time taken is the first event into the target: from then on the plug was in, as far as the
scorer can tell. An earlier time can only turn a verdict into a false alarm, so the choice
counts against the Monitor, as ADR 029's ties do.

**Its time.** The insertion event has no header, so its receive time (wall clock) is mapped onto
sim time through the wrench messages of the same bag, each a pair of a receive time and a header
stamp in sim time:
1. the pair just before the event's receive time and the pair just after it (for pairs received
   at that very time, the earliest and the latest of their stamps);
2. linear interpolation between their stamps;
3. τ, the tolerance, is the larger of 0.25 s and three times the sim-time gap between them;
4. no mapping when either pair is more than 1 s of wall time from the event, or when their
   stamps run backwards. The judge then falls back to judging the trial as a whole.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from cellrt.eval.referee import RefereeTrial, port_parts

MAX_PAIR_DISTANCE_NS = 1_000_000_000  # ADR 029: 1 s of wall time
MAX_INT64 = 1 << 63  # rosbag2 times are int64 ns
MIN_TOLERANCE_S = 0.25
GAP_TOLERANCE_FACTOR = 3
NOT_A_DUMP = "bag dump: not a bag dump"


class ScoringInputError(ValueError):
    """A bag dump this reader will not use; the message is fixed and never quotes the file."""


def referee_trial(score: object, dump: object, target: tuple[str, str]) -> RefereeTrial:
    """The referee of one trial from its `scoring.yaml` entry (None or anything malformed gives
    no scores) and its bag dump (ScoringInputError if malformed)."""
    insertions, contacts, pairs = _dump(dump)
    last = port_parts(insertions[-1][1]) if insertions else None
    sim_s = tau = None
    if last is not None and last[:2] == target:
        first = next(recv for recv, port in insertions if port_parts(port)[:2] == target)
        mapped = sim_time(first, pairs)
        if mapped is not None:
            sim_s, tau = mapped
    return RefereeTrial(
        target=target,
        last_port=last,
        insertion_sim_s=sim_s,
        tolerance_s=tau,
        contact_messages=len(contacts),
        contacts_score=_category(score, "contacts"),
        force_penalty=_category(score, "insertion force"),
        tier3=_tier(score, "tier_3"),
        total=_total(score),
    )


def sim_time(recv_ns: int, pairs: Sequence[tuple[int, int]]) -> tuple[float, float] | None:
    """The sim time (s) of a receive time (ns), and its tolerance τ (s); None if unmapped."""
    ordered = sorted(pairs)
    recvs = [recv for recv, _ in ordered]
    before = bisect_right(recvs, recv_ns) - 1
    after = bisect_left(recvs, recv_ns)
    if before < 0 or after >= len(ordered):
        return None
    if before > after:  # pairs received at the event's very time: the earliest and latest stamp
        before, after = after, before
    (r0, s0), (r1, s1) = ordered[before], ordered[after]
    if recv_ns - r0 > MAX_PAIR_DISTANCE_NS or r1 - recv_ns > MAX_PAIR_DISTANCE_NS or s1 < s0:
        return None
    sim_ns = s0 if r1 == r0 else s0 + (recv_ns - r0) * (s1 - s0) / (r1 - r0)
    tau = max(MIN_TOLERANCE_S, GAP_TOLERANCE_FACTOR * (s1 - s0) / 1e9)
    return sim_ns / 1e9, tau


def _dump(dump: object) -> tuple[list[tuple[int, str]], list[int], list[tuple[int, int]]]:
    try:
        if not isinstance(dump, Mapping) or dump.get("version") != 1:
            raise ScoringInputError(NOT_A_DUMP)
        insertions = [(_int(e["recv_ns"]), _str(e["port"])) for e in _list(dump["insertions"])]
        contacts = [(_int(e["recv_ns"]), _int(e["count"])) for e in _list(dump["contacts"])]
        pairs = [_pair(p) for p in _list(dump["pairs"])]
    except (KeyError, TypeError, IndexError):
        raise ScoringInputError(NOT_A_DUMP) from None
    return insertions, [count for _, count in contacts], pairs


def _pair(value: object) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ScoringInputError(NOT_A_DUMP)
    return _int(value[0]), _int(value[1])


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ScoringInputError(NOT_A_DUMP)
    return value


def _int(value: object) -> int:
    """A bag's time in ns, or a count: a non-negative int64, so no arithmetic on it overflows a
    float."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < MAX_INT64:
        raise ScoringInputError(NOT_A_DUMP)
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ScoringInputError(NOT_A_DUMP)
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)  # an int too large for a float raises OverflowError
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _tier(score: object, tier: str) -> float | None:
    try:
        return _number(score[tier]["score"])  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        return None


def _category(score: object, name: str) -> float | None:
    try:
        return _number(score["tier_2"]["categories"][name]["score"])  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        return None


def _total(score: object) -> float | None:
    tiers = [_tier(score, t) for t in ("tier_1", "tier_2", "tier_3")]
    return None if any(t is None for t in tiers) else sum(t for t in tiers if t is not None)
