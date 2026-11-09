"""The cell runtime as an aic_model policy: launch with `policy:=adapters.aic.RuntimePolicy`
(aic_model loads the class named like the module's last part). ROS-only and thin: it maps the
node to a `NodePort` and hands each goal to the ROS-free policy body (`run.run_goal`).

Runs are stored in the SQLite file named by `CELLRT_STORE` (default `runs/aic.sqlite`). One
policy lives for a whole engine run (aic_model creates it on its first configure), so it numbers
the goals it receives: goal N of the engine config named by `CELLRT_SESSION` (default `aic`) is
the scenario `<session>/goal<N>/<task id>`. `CELLRT_SETTING` (A, B or C) and `CELLRT_PLANNER`
(rule, fixed or llm) choose the tree and setting C's planner; they are read on the first goal,
and a bad value refuses every goal, before the arm moves, with a fixed message.
Nothing heavy is imported here, so loading stays inside aic_model's GetState timeout.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from aic_model.policy import Policy

from adapters.aic.convert import core_task
from adapters.aic.ros_port import RosNodePort
from adapters.aic.run import (
    PolicySetupError,
    goal_scenario_id,
    policy_setup,
    run_goal,
    session_label,
)
from cellrt.executor.trees import Setting
from cellrt.planner.base import Planner
from cellrt.runtime.store import EventStore

DEFAULT_STORE = Path("runs") / "aic.sqlite"


class RuntimePolicy(Policy):
    def __init__(self, parent_node: Any) -> None:
        super().__init__(parent_node)
        self._store_path = Path(os.environ.get("CELLRT_STORE", str(DEFAULT_STORE)))
        self._session = session_label(os.environ.get("CELLRT_SESSION"))
        # insert_cable runs in aic_model's action thread, a new one per goal (ADR 012).
        self._goals_lock = threading.Lock()
        self._goals = 0
        # The setting and planner, made on the first goal that can use them and kept for the
        # engine run (a model planner's call cap counts over every goal). The engine sends goals
        # one after another, so the planner and its cap are never used by two goals at once.
        self._setup: tuple[Setting, Planner | None] | None = None

    def insert_cable(
        self, task: Any, get_observation: Any, move_robot: Any, send_feedback: Any
    ) -> bool:
        """Always returns a bool, never raises: aic_model's execute loop needs a result, and a
        traceback could carry upstream text. Failures are logged by exception type only."""
        with self._goals_lock:  # every goal takes its number, even one that fails early
            self._goals += 1
            goal = self._goals
        try:
            setting, planner = self._settings()
        except PolicySetupError as ex:  # a fixed message; the arm has not moved
            self.get_logger().error(f"cell-runtime goal refused: {ex}")
            return False
        except Exception as ex:  # anything else: its text is not ours to log
            self._log_failure("setup", ex)
            return False
        port: RosNodePort | None = None
        try:
            port = RosNodePort(self._parent_node, move_robot, send_feedback)
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            core = core_task(task)
            scenario = goal_scenario_id(self._session, goal, core.id)
            with EventStore(self._store_path) as store:
                return run_goal(
                    port, core, store, scenario_id=scenario, setting=setting, planner=planner
                )
        except Exception as ex:  # before or outside a run: run_goal records its own stops
            self._log_failure("goal", ex)
            return False
        finally:
            if port is not None:
                try:
                    port.close()
                except Exception as ex:
                    self._log_failure("close", ex)

    def _settings(self) -> tuple[Setting, Planner | None]:
        with self._goals_lock:
            if self._setup is None:
                self._setup = policy_setup(os.environ)
            return self._setup

    def _log_failure(self, what: str, ex: Exception) -> None:
        self.get_logger().error(f"cell-runtime {what} failed: {type(ex).__name__}")
