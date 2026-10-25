"""Evidence-backed alert state, independent of delivery and notification reads."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class AlertLifecycle(BaseModel):
    state: Literal["ACTIVE", "RESOLVED", "UNKNOWN"] = "UNKNOWN"
    resolvedAt: datetime | None = None
    resolutionObservedAt: datetime | None = None
