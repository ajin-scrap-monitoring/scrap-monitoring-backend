"""Edge authentication dependencies and security helpers."""

from fastapi import Header, HTTPException, status

from src.app.core.config import get_settings


async def verify_edge_api_key(
    x_edge_api_key: str | None = Header(default=None, alias="X-Edge-API-Key"),
) -> str:
    """Verify Edge device API key from request header.

    Raises:
        HTTPException: 401 Unauthorized if missing or invalid.
    """
    settings = get_settings()

    if not x_edge_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Edge-API-Key header",
        )

    if x_edge_api_key != settings.EDGE_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Edge device API key",
        )

    return x_edge_api_key
