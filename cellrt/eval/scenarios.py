"""Scenarios: what the cell is asked to do, and (later) which faults are injected."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from cellrt.eval.faults import FaultSpec
from cellrt.runtime.task import Task


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    task: Task = Field(default_factory=Task)
    faults: list[FaultSpec] = Field(default_factory=list)


class ScenarioFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenarios: list[Scenario]

    @field_validator("scenarios")
    @classmethod
    def _unique_ids(cls, scenarios: list[Scenario]) -> list[Scenario]:
        """Every run, attempt and metric is keyed by the scenario id."""
        seen: set[str] = set()
        for scenario in scenarios:
            if scenario.id in seen:
                raise PydanticCustomError(
                    "duplicate_scenario_id",
                    "scenario id {id} appears more than once",
                    {"id": scenario.id},
                )
            seen.add(scenario.id)
        return scenarios


def load_scenarios(path: Path) -> list[Scenario]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ScenarioFile.model_validate(data).scenarios
