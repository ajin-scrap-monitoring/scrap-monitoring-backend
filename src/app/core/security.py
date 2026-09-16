"""Edge authentication dependencies and security helpers."""

import hmac

from fastapi import Header, HTTPException, status

from src.app.core.config import get_settings


async def verify_edge_api_key(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_edge_api_key: str | None = Header(default=None, alias="X-Edge-API-Key"),
) -> str:
    """Verify Edge device API key or Bearer token from request headers.

    Supports both 'Authorization: Bearer <token>' (standard edge contract)
    and 'X-Edge-API-Key: <token>' (legacy header).

    Raises:
        HTTPException: 401 Unauthorized if missing or invalid.
    """
    settings = get_settings()

    token: str | None = None
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        else:
            token = authorization.strip()
    elif x_edge_api_key:
        token = x_edge_api_key.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization or X-Edge-API-Key header",
        )

    if not hmac.compare_digest(token, settings.EDGE_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Edge device authentication token",
        )

    return token
