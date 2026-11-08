"""What loading the policy brings in: aic_model imports `adapters.aic.RuntimePolicy` inside its
GetState timeout, so the adapter must not import torch or lerobot at module level (RunACT
loads them lazily for the same reason), and the policy body must not need ROS at all."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parents[1] / "adapters" / "aic"
HEAVY = ("torch", "lerobot", "numpy")
ROS = ("rclpy", "tf2_ros", "geometry_msgs", "action_msgs", "aic_model", "aic_control_interfaces")


def _module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:  # module level only: a lazy import inside a function is allowed
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_no_adapter_module_imports_torch_lerobot_or_numpy_at_module_level() -> None:
    modules = sorted(ADAPTER_DIR.glob("*.py"))
    assert any(p.name == "RuntimePolicy.py" for p in modules)

    offenders = {p.name: _module_level_imports(p) & set(HEAVY) for p in modules}
    assert {name: found for name, found in offenders.items() if found} == {}


def test_the_policy_body_imports_without_ros_or_heavy_modules() -> None:
    code = (
        "import sys\n"
        "import adapters.aic.run, adapters.aic.world, adapters.aic.skills\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {HEAVY + ROS!r})\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
