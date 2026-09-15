"""Session and Authentication schemas conforming to proposal/openapi.yaml."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UserRole = Literal["viewer", "administrator"]


class User(BaseModel):
    """Authenticated user profile."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Unique user identifier", examples=["admin", "operator"])
    displayName: str = Field(
        ..., description="Display name of the user", examples=["시스템 관리자"]
    )
    role: UserRole = Field(..., description="User access control role")


class LoginRequest(BaseModel):
    """User credentials for session authentication."""

    model_config = ConfigDict(from_attributes=True)

    username: str = Field(min_length=1, description="Username/ID")
    password: str = Field(min_length=1, description="Password")
    persistent: bool = Field(
        default=False,
        description="Requests a persistent session within the server-defined maximum lifetime",
    )


class Session(BaseModel):
    """Authenticated browser session details."""

    model_config = ConfigDict(from_attributes=True)

    user: User
    expiresAt: datetime = Field(description="ISO 8601 UTC timestamp of session expiration")
    csrfToken: str = Field(min_length=1, description="Cross-Site Request Forgery (CSRF) token")
