"""Internal upstream validation evidence; never a Dashboard storage DTO."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class CapacityWarning(BaseModel):
    code: str = Field(pattern=r"^[A-Z0-9_]+$", min_length=1, max_length=100)


class PolicyCapacityValidation(BaseModel):
    status: Literal["insufficient_capacity", "estimated_fit", "unknown"]
    evaluated_at: AwareDatetime
    policy_applied: bool = Field(strict=True)
    warnings: list[CapacityWarning] = Field(max_length=100)

    @model_validator(mode="after")
    def require_applied(self) -> "PolicyCapacityValidation":
        if not self.policy_applied:
            raise ValueError("A policy PUT must confirm that the setting was applied")
        return self
