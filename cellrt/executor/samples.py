"""The one place where a world sample becomes a `signal_sampled` event, and back.

The runner logs every sample; the Monitor judges the sample as it was logged (`signals_of` the
run state's last sample), not a fresh `world.signals()`. In the mock the two are the same
between two world steps, but a world fed by callbacks (the AIC adapter) can change in between;
judging the logged sample keeps every verdict checkable from the log.
"""

from __future__ import annotations

from cellrt.runtime.events import SignalSampled
from cellrt.world.base import Signals


def sampled(step: int, s: Signals) -> SignalSampled:
    """The event for one sample: what the Monitor and the skills saw on this step."""
    return SignalSampled(
        step=step,
        height_mm=s.height_mm,
        depth_mm=s.depth_mm,
        xy_offset_mm=s.xy_offset_mm,
        xy_error_mm=s.xy_error_mm,
        raw_force_n=s.raw_force_n,
        tare_offset_n=s.tare_offset_n,
        force_n=s.force_n,
        torque_nm=s.torque_nm,
        wrench_valid=s.wrench_valid,
        force_over_limit_s=s.force_over_limit_s,
    )


def signals_of(event: SignalSampled) -> Signals:
    """The sample a `signal_sampled` event recorded."""
    if event.xy_offset_mm is None:
        raise ValueError("a sample logged before xy_offset_mm was recorded cannot be judged")
    return Signals(
        height_mm=event.height_mm,
        xy_offset_mm=event.xy_offset_mm,
        raw_force_n=event.raw_force_n,
        tare_offset_n=event.tare_offset_n,
        torque_nm=event.torque_nm,
        wrench_valid=event.wrench_valid,
        force_over_limit_s=event.force_over_limit_s,
    )
