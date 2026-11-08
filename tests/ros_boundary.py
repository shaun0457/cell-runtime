"""Static check that a module imports nothing from ROS, AIC or the adapters package."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "cellrt"

_BANNED_TOP_LEVEL = frozenset(
    {
        "rclpy",
        "rcl_interfaces",
        "rosgraph_msgs",
        "builtin_interfaces",
        "tf2_ros",
        "launch",
        "launch_ros",
        "ament_index_python",
        "message_filters",
        "cv_bridge",
        "adapters",
    }
)
_BANNED_PREFIXES = ("aic_", "rosidl_")
_BANNED_SUFFIXES = ("_msgs", "_srvs", "_interfaces")
_DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})


@dataclass(frozen=True)
class BannedImport:
    line: int
    module: str


def is_banned(module: str) -> bool:
    top = module.split(".", 1)[0]
    return (
        top in _BANNED_TOP_LEVEL
        or top.startswith(_BANNED_PREFIXES)
        or top.endswith(_BANNED_SUFFIXES)
    )


def find_banned_imports(source: str) -> list[BannedImport]:
    hits: list[BannedImport] = []
    for node in ast.walk(ast.parse(source)):
        for module in _imported_modules(node):
            if is_banned(module):
                hits.append(BannedImport(line=node.lineno, module=module))
    return sorted(hits, key=lambda h: h.line)


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.level == 0 and node.module else []
    if isinstance(node, ast.Call) and _is_dynamic_importer(node.func) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return [first.value]
    return []


def _is_dynamic_importer(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in _DYNAMIC_IMPORTERS
    if isinstance(func, ast.Attribute):
        return func.attr in _DYNAMIC_IMPORTERS
    return False
