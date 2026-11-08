"""Import every cellrt module while any ROS/AIC import raises. Run as a script in a subprocess.

Modules are found by walking the files, not the package tree, so a directory that is missing its
`__init__.py` is still imported (as a namespace package) instead of being skipped silently.
"""

from __future__ import annotations

import importlib
import importlib.abc
import sys

from ros_boundary import CORE_DIR, is_banned


class _BlockRos(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):  # type: ignore[no-untyped-def]
        if is_banned(fullname):
            raise ImportError(f"ROS-free core imported {fullname!r}")
        return None


def _module_name(path) -> str:  # type: ignore[no-untyped-def]
    parts = list(path.relative_to(CORE_DIR.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def main() -> int:
    sys.meta_path.insert(0, _BlockRos())
    sys.path.insert(0, str(CORE_DIR.parent))
    names = [_module_name(p) for p in sorted(CORE_DIR.rglob("*.py"))]
    for name in names:
        importlib.import_module(name)
    print(f"imported {len(names)} modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
