"""OpenAPI declarations for the existing HTTP and safe media error handlers."""

from http import HTTPStatus
from typing import Any

from src.app.schemas.errors import (
    HTTPErrorResponse,
    MediaErrorResponse,
    RequestValidationErrorResponse,
)

# Status codes deliberately forwarded by MediaClient, plus local transport failures.
MEDIA_ERROR_CODES = (400, 404, 409, 410, 415, 416, 429, 502, 503, 504)


def error_responses(*codes: int, media: bool = False) -> dict[int | str, dict[str, Any]]:
    """Declare only the callers' error statuses, without changing runtime behavior."""
    result: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        model: Any = HTTPErrorResponse
        if code == 422:
            model = HTTPErrorResponse | RequestValidationErrorResponse
        elif media and code in MEDIA_ERROR_CODES:
            # Local missing-event checks and authentication storage may use HTTPException.
            model = (
                HTTPErrorResponse | MediaErrorResponse if code in (404, 503) else MediaErrorResponse
            )
        result[code] = {"model": model, "description": HTTPStatus(code).phrase}
    return result
