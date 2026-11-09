"""Wall time, for the one thing in the core that waits on the outside world: a planner call's
deadline, and how long a call has waited (ADR 016, ADR 020). Everything else runs on the
simulated clock. `ExecEnv.wall_clock` defaults to this; a test passes a fake.
"""

from __future__ import annotations

import time


def wall_now() -> float:
    """Monotonic wall time in seconds."""
    return time.monotonic()  # noqa: TID251
