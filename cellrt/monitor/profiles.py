"""What a skill declares about itself, so the Monitor knows which rules apply while it runs.

Kept apart from the classifier (and free of imports) so skills can depend on it without
depending on the Monitor.

- motion: moves the arm; sensor dropout and the force limit apply
- insertion: moves the plug into the port; the insertion rules apply as well
- sensor_wait: holds still waiting for the sensor; no dropout rule
- unload: backs the plug out to relieve force; its time over the force limit counts, but it is
  not preempted for force (stopping it would stop the one thing that unloads)
"""

from __future__ import annotations

from typing import Literal, get_args

MonitorProfile = Literal["motion", "insertion", "sensor_wait", "unload"]
MONITOR_PROFILES: frozenset[str] = frozenset(get_args(MonitorProfile))
