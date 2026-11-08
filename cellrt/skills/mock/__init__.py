from cellrt.skills.mock.skills import (
    Align,
    Approach,
    InsertAct,
    InsertScripted,
    Retract,
    Verify,
    WaitSensor,
)
from cellrt.skills.registry import SkillRegistry


def mock_registry() -> SkillRegistry:
    return SkillRegistry(
        [Approach(), Align(), InsertScripted(), InsertAct(), Verify(), Retract(), WaitSensor()]
    )


__all__ = [
    "Align",
    "Approach",
    "InsertAct",
    "InsertScripted",
    "Retract",
    "Verify",
    "WaitSensor",
    "mock_registry",
]
