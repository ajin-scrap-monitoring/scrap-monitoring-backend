"""OpenAPI declarations and JSON-safe request validation error responses."""

import math
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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


async def request_validation_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Preserve validation details while making rejected non-finite inputs JSON-safe."""
    assert isinstance(exc, RequestValidationError)
    details = jsonable_encoder(
        exc.errors(),
        custom_encoder={float: lambda value: value if math.isfinite(value) else str(value)},
    )
    return JSONResponse(status_code=422, content={"detail": details})
