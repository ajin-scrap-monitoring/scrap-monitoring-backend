"""Session-protected daily recording JSON API; Nginx serves HLS and MP4."""

import re
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator

from src.app.api.v1.media_control import CurrentSession, require_media_admin
from src.app.schemas.daily_recording import DailyPage, DailyPolicy, DailyRecording, DailyStatus
from src.app.schemas.session import Session
from src.app.services.daily_recording_service import daily_recording_service


async def disable_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def validate_date(value: object) -> object:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use a Korean calendar date in YYYY-MM-DD format")
    return value


CalendarDate = Annotated[date, BeforeValidator(validate_date)]
router = APIRouter(
    prefix="/api/v2/recordings/daily",
    tags=["Daily Recordings"],
    dependencies=[Depends(disable_cache)],
)


async def require_daily_csrf(
    session: CurrentSession,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Session:
    """Viewers can prepare their downloads; all mutations need the session CSRF nonce."""
    if csrf_token is None or csrf_token != session.csrfToken:
        raise HTTPException(403, "Invalid or missing CSRF token")
    return session


@router.get("/policy", response_model=DailyPolicy)
async def get_daily_policy(session: CurrentSession) -> DailyPolicy:
    return await daily_recording_service.policy()


@router.put("/policy", response_model=DailyPolicy, dependencies=[Depends(require_media_admin)])
async def put_daily_policy(policy: DailyPolicy) -> DailyPolicy:
    return await daily_recording_service.policy(policy)


@router.get("", response_model=DailyPage)
async def list_daily_recordings(
    session: CurrentSession,
    from_date: CalendarDate | None = None,
    to_date: CalendarDate | None = None,
    status: DailyStatus | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=4096),
) -> DailyPage:
    if from_date and to_date and from_date >= to_date:
        raise HTTPException(422, "from_date must precede to_date")
    params: dict[str, str | int] = {"limit": limit}
    for key, value in {
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "status": status,
        "cursor": cursor,
    }.items():
        if value is not None:
            params[key] = value
    return await daily_recording_service.list(params)


@router.get("/{recording_id}", response_model=DailyRecording)
async def get_daily_recording(recording_id: UUID, session: CurrentSession) -> DailyRecording:
    return await daily_recording_service.detail(recording_id)


@router.post(
    "/{recording_id}/exports",
    response_model=DailyRecording,
    responses={202: {"model": DailyRecording}},
    dependencies=[Depends(require_daily_csrf)],
)
async def prepare_daily_download(recording_id: UUID) -> JSONResponse:
    code, item = await daily_recording_service.export(recording_id)
    return JSONResponse(
        status_code=code,
        content=item.model_dump(mode="json"),
        headers={
            "Location": f"/api/v2/recordings/daily/{recording_id}",
            "Cache-Control": "no-store",
        },
    )


@router.delete("/{recording_id}", status_code=204, dependencies=[Depends(require_media_admin)])
async def delete_daily_recording(recording_id: UUID) -> Response:
    await daily_recording_service.delete(recording_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
