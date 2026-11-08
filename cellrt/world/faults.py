"""Concrete faults the mock world applies. Parameters are already drawn (see eval/faults.py).

A fault knows when it is active from world time only; the world turns an active fault into an
effect. Nothing here is visible through `Signals`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cellrt.monitor.classifier import FailureClass


@dataclass(frozen=True)
class ControllerStall:
    """The arm controller stops executing commands for a while (the plug holds still)."""

    fault_id: str
    onset_s: float
    duration_s: float
    kind: str = "controller_stall"
    label: FailureClass = FailureClass.TIMEOUT

    def is_active(self, t: float) -> bool:
        return self.onset_s <= t < self.onset_s + self.duration_s

    @property
    def params(self) -> Mapping[str, Any]:
        return {"onset_s": self.onset_s, "duration_s": self.duration_s}


Fault = ControllerStall
