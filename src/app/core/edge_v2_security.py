"""Device-scoped v2 credentials, isolated from the legacy shared edge key."""

import hmac
from typing import Annotated, Self

from fastapi import Header
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from src.app.schemas.edge_v2 import Identifier


class EdgeV2Device(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    site_id: Identifier
    edge_id: Identifier
    sensor_ids: tuple[Identifier, Identifier]
    token: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def valid_device(self) -> Self:
        import re

        if len(set(self.sensor_ids)) != 2:
            raise ValueError("Two distinct sensor IDs are required")
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", self.token.get_secret_value()):
            raise ValueError("Device token must use the Bearer token character set")
        return self


class EdgeV2Error(Exception):
    def __init__(
        self, status: int, code: str, field: str | None = None, retryable: bool = False
    ) -> None:
        self.status = status
        self.code = code
        self.field = field
        self.retryable = retryable


async def authenticate_device(
    authorization: Annotated[str | None, Header()] = None,
) -> EdgeV2Device:
    from src.app.core.config import get_settings

    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise EdgeV2Error(401, "AUTHENTICATION_FAILED")
    # Compare bytes so malformed non-ASCII input cannot raise a TypeError.
    candidate = parts[1].encode("utf-8")
    matched: EdgeV2Device | None = None
    for device in get_settings().EDGE_V2_DEVICES:
        if hmac.compare_digest(candidate, device.token.get_secret_value().encode("ascii")):
            matched = device
    if matched is None:
        raise EdgeV2Error(401, "AUTHENTICATION_FAILED")
    return matched
