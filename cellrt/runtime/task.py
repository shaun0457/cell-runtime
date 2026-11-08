"""The task the cell is asked to do. Fields mirror the AIC `Task` so the adapter maps 1:1."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Task(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = "insert-sfp-0"
    cable_type: str = "sfp"
    cable_name: str = "cable_0"
    plug_type: str = "sfp"
    plug_name: str = "plug"
    port_type: str = "sfp"
    port_name: str = "port_0"
    target_module_name: str = "nic_card_0"
    # Finite: JSON has no inf, so an infinite limit could not be read back from the log.
    time_limit: float = Field(
        default=60.0, gt=0, allow_inf_nan=False, description="seconds of (simulated) time"
    )
