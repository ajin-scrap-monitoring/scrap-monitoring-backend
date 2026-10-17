"""Normalized internal input, explicitly distinct from an edge assessment."""

from typing import Literal

from pydantic import BaseModel, Field

from src.app.schemas.edge_v2 import Identifier, UtcTime


class FillObservation(BaseModel):
    source: Literal["EDGE_STATE", "HEIGHT_SECTIONS"]
    site_id: Identifier
    edge_id: Identifier
    config_revision: Identifier
    message_id: Identifier
    observation_id: Identifier
    generated_at: UtcTime
    received_at: UtcTime
    observed_to: UtcTime | None
    rule_version: Identifier | None
    method_version: Identifier | None
    value_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    data_state: Literal["AVAILABLE", "LIMITED", "UNAVAILABLE", "STALE", "UNKNOWN", "NO_DATA"]
    reason_codes: list[str]
    scope_token: str | None = None

    @property
    def assessment_id(self) -> str:
        """Compatibility identifier for the existing internal operational cursor/link column."""
        return self.observation_id
