"""V2 receiver routes with compact, credential-free contract errors."""

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1.session import get_session
from src.app.core.edge_v2_security import EdgeV2Device, EdgeV2Error, authenticate_device
from src.app.db.session import get_db_session
from src.app.schemas.edge_v2 import (
    Assessment,
    Configuration,
    DeviceStatus,
    Envelope,
    Identifier,
    Measurement,
    StatusReport,
    StorageAck,
    StorageError,
)
from src.app.services.edge_v2_service import accept_message, device_status


def readable_message_id(value: Any) -> str | None:
    message_id = value.get("message_id") if isinstance(value, dict) else None
    return message_id if isinstance(message_id, str) and 0 < len(message_id) <= 128 else None


class ContractRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except (EdgeV2Error, RequestValidationError, HTTPException) as exc:
                message_id = None
                if isinstance(exc, RequestValidationError):
                    message_id = readable_message_id(exc.body)
                    error = exc.errors()[0]
                    # Never echo validation input, messages, or attacker-controlled keys.
                    location = error.get("loc", ())
                    if error.get("type") == "extra_forbidden":
                        location = location[:-1]
                    field = ".".join(
                        str(part) for part in location if part not in ("body", "header")
                    )
                    exc = EdgeV2Error(422, "SCHEMA_VALIDATION_FAILED", field or None)
                elif isinstance(exc, HTTPException):
                    if request.method != "POST" or exc.status_code != 400:
                        raise
                    exc = EdgeV2Error(400, "SCHEMA_VALIDATION_FAILED")
                else:
                    message_id = getattr(request.state, "edge_message_id", None)
                headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
                if exc.retryable:
                    headers = {"Retry-After": "1"}
                return JSONResponse(
                    status_code=exc.status,
                    content={
                        "message_id": message_id,
                        "accepted": False,
                        "error": {"code": exc.code, "retryable": exc.retryable, "field": exc.field},
                    },
                    headers=headers,
                )

        return wrapped


async def validate_transport(
    request: Request,
    idempotency_key: Annotated[Identifier, Header(alias="Idempotency-Key")],
) -> None:
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise EdgeV2Error(415, "UNSUPPORTED_CONTENT_ENCODING")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise EdgeV2Error(415, "UNSUPPORTED_MEDIA_TYPE")
    try:
        json.loads((await request.body()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EdgeV2Error(400, "SCHEMA_VALIDATION_FAILED") from None


router = APIRouter(prefix="/api/edge/v2", tags=["Edge v2"], route_class=ContractRoute)
Database = Annotated[AsyncSession, Depends(get_db_session)]
Device = Annotated[EdgeV2Device, Depends(authenticate_device)]
RECEIVER_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": StorageAck, "description": "Identical replay; original storage receipt"},
    **{code: {"model": StorageError} for code in (400, 401, 403, 409, 415, 422, 503)},
}


async def receive(
    payload: Envelope, request: Request, response: Response, db: AsyncSession, device: EdgeV2Device
) -> StorageAck:
    original: dict[str, Any] = await request.json()
    request.state.edge_message_id = payload.message_id
    if request.headers.get("idempotency-key") != payload.message_id:
        raise EdgeV2Error(422, "SCHEMA_VALIDATION_FAILED", "Idempotency-Key")
    # Standard JSON disallows NaN/Infinity, including fields not consumed by validators.
    try:
        json.dumps(original, allow_nan=False)
    except ValueError:
        raise EdgeV2Error(422, "SCHEMA_VALIDATION_FAILED") from None
    ack = await accept_message(db, payload, original, device)
    response.status_code = 200 if ack.duplicate else 201
    return ack


@router.post(
    "/measurements",
    response_model=StorageAck,
    status_code=201,
    responses=RECEIVER_RESPONSES,
    dependencies=[Depends(validate_transport)],
)
async def receive_measurement(
    payload: Measurement, request: Request, response: Response, db: Database, device: Device
) -> StorageAck:
    return await receive(payload, request, response, db, device)


@router.post(
    "/assessments",
    response_model=StorageAck,
    status_code=201,
    responses=RECEIVER_RESPONSES,
    dependencies=[Depends(validate_transport)],
)
async def receive_assessment(
    payload: Assessment, request: Request, response: Response, db: Database, device: Device
) -> StorageAck:
    return await receive(payload, request, response, db, device)


@router.post(
    "/status",
    response_model=StorageAck,
    status_code=201,
    responses=RECEIVER_RESPONSES,
    dependencies=[Depends(validate_transport)],
)
async def receive_status(
    payload: StatusReport, request: Request, response: Response, db: Database, device: Device
) -> StorageAck:
    return await receive(payload, request, response, db, device)


@router.post(
    "/configurations",
    response_model=StorageAck,
    status_code=201,
    responses=RECEIVER_RESPONSES,
    dependencies=[Depends(validate_transport)],
)
async def receive_configuration(
    payload: Configuration, request: Request, response: Response, db: Database, device: Device
) -> StorageAck:
    return await receive(payload, request, response, db, device)


@router.get(
    "/devices/{site_id}/{edge_id}/status",
    response_model=DeviceStatus,
    dependencies=[Depends(get_session)],
)
async def read_device_status(
    site_id: Annotated[Identifier, Path()], edge_id: Annotated[Identifier, Path()], db: Database
) -> DeviceStatus:
    return await device_status(db, site_id, edge_id)
