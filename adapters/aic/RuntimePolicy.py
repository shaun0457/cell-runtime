"""The cell runtime as an aic_model policy: launch with `policy:=adapters.aic.RuntimePolicy`
(aic_model loads the class named like the module's last part). ROS-only and thin: it maps the
node to a `NodePort` and hands each goal to the ROS-free policy body (`run.run_goal`).

Runs are stored in the SQLite file named by `CELLRT_STORE` (default `runs/aic.sqlite`).
Nothing heavy is imported here, so loading stays inside aic_model's GetState timeout.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from aic_model.policy import Policy

from adapters.aic.convert import core_task
from adapters.aic.ros_port import RosNodePort
from adapters.aic.run import run_goal
from cellrt.runtime.store import EventStore

DEFAULT_STORE = Path("runs") / "aic.sqlite"


class RuntimePolicy(Policy):
    def __init__(self, parent_node: Any) -> None:
        super().__init__(parent_node)
        self._store_path = Path(os.environ.get("CELLRT_STORE", str(DEFAULT_STORE)))

    def insert_cable(
        self, task: Any, get_observation: Any, move_robot: Any, send_feedback: Any
    ) -> bool:
        """Always returns a bool, never raises: aic_model's execute loop needs a result, and a
        traceback could carry upstream text. Failures are logged by exception type only."""
        port: RosNodePort | None = None
        try:
            port = RosNodePort(self._parent_node, move_robot)
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            with EventStore(self._store_path) as store:
                return run_goal(port, core_task(task), store)
        except Exception as ex:  # an unfinished run stays in the store and is rerun
            self._log_failure("goal", ex)
            return False
        finally:
            if port is not None:
                try:
                    port.close()
                except Exception as ex:
                    self._log_failure("close", ex)

    def _log_failure(self, what: str, ex: Exception) -> None:
        self.get_logger().error(f"cell-runtime {what} failed: {type(ex).__name__}")
