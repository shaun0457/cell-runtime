"""The core (cellrt/) must import nothing from ROS or AIC (CLAUDE.md, design principle 3)."""

import subprocess
import sys
import textwrap
from pathlib import Path

from ros_boundary import CORE_DIR, find_banned_imports


def test_checker_flags_every_way_of_importing_ros() -> None:
    source = textwrap.dedent(
        """
        import os
        import rclpy
        import rclpy.node as node
        from geometry_msgs.msg import Pose
        from aic_model.policy import Policy
        import importlib
        importlib.import_module("tf2_ros")
        __import__("sensor_msgs.msg")
        from . import sibling
        from adapters.aic import world
        """
    )

    hits = find_banned_imports(source)

    assert [h.module for h in hits] == [
        "rclpy",
        "rclpy.node",
        "geometry_msgs.msg",
        "aic_model.policy",
        "tf2_ros",
        "sensor_msgs.msg",
        "adapters.aic",
    ]
    assert [h.line for h in hits] == [3, 4, 5, 6, 8, 9, 11]


def test_checker_allows_ordinary_imports() -> None:
    source = "import asyncio\nfrom pydantic import BaseModel\nimport py_trees\nimport rclpy_like\n"

    assert find_banned_imports(source) == []


def test_core_has_no_ros_imports() -> None:
    offenders = [
        f"{path.relative_to(CORE_DIR.parent)}:{hit.line}: {hit.module}"
        for path in sorted(CORE_DIR.rglob("*.py"))
        for hit in find_banned_imports(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_every_core_module_imports_with_ros_blocked() -> None:
    script = Path(__file__).with_name("import_core_without_ros.py")

    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr
    module_files = len(list(CORE_DIR.rglob("*.py")))
    assert result.stdout.strip() == f"imported {module_files} modules"
