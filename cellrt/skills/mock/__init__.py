from cellrt.skills.mock.skills import Align, Approach, InsertScripted, Verify
from cellrt.skills.registry import SkillRegistry


def mock_registry() -> SkillRegistry:
    return SkillRegistry([Approach(), Align(), InsertScripted(), Verify()])


__all__ = ["Align", "Approach", "InsertScripted", "Verify", "mock_registry"]
