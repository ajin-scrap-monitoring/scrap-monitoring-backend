"""JSON display precision, separate from stored measurements and operational calculations."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import Field, PlainSerializer


def rounded_percent(value: float) -> float:
    """Round the third decimal half up; retain a JSON number, not a formatted string."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# Python model attributes/dumps retain their precision; only JSON output is rounded.
DisplayPercent = Annotated[
    float,
    PlainSerializer(
        rounded_percent,
        return_type=Annotated[float, Field(ge=0, le=100)],
        when_used="json-unless-none",
    ),
]
