from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cellrt.eval.scenarios import Scenario
from cellrt.runtime.store import EventStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "events.sqlite") as s:
        yield s


@pytest.fixture
def baseline() -> Scenario:
    return Scenario(id="baseline")
