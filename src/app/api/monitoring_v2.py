"""Session-authenticated v2 dashboard snapshot and bounded-cadence SSE."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1.session import get_session
from src.app.db.session import get_db_session
from src.app.schemas.monitoring_v2 import MonitoringSnapshotV2, MonitoringSource
from src.app.schemas.session import Session
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from src.app.services.session_service import SESSION_COOKIE_NAME

router = APIRouter(prefix="/api/v2/monitoring", tags=["Monitoring v2"])
Database = Annotated[AsyncSession, Depends(get_db_session)]
CurrentSession = Annotated[Session, Depends(get_session)]


def get_source(
    pit_id: str = Query(default="pit-01", min_length=1, max_length=128),
) -> MonitoringSource:
    return monitoring_source(pit_id)


Source = Annotated[MonitoringSource, Depends(get_source)]


async def read_snapshot(db: AsyncSession, source: MonitoringSource) -> MonitoringSnapshotV2:
    """Release the read transaction between frames, including on errors/cancellation."""
    try:
        return await build_snapshot(db, source)
    finally:
        await db.rollback()


@router.get("/snapshot", response_model=MonitoringSnapshotV2)
async def snapshot(
    response: Response, session: CurrentSession, source: Source, db: Database
) -> MonitoringSnapshotV2:
    response.headers["Cache-Control"] = "no-store"
    return await read_snapshot(db, source)


async def snapshot_events(
    request: Request,
    db: AsyncSession,
    source: MonitoringSource,
    session_id: str | None,
    initial: MonitoringSnapshotV2,
) -> AsyncGenerator[str]:
    """Poll committed JSON metadata only; no queues/tasks grow with a slow consumer."""
    previous = initial.snapshotId
    yield f"event: monitoring.snapshot\ndata: {initial.model_dump_json()}\n\n"
    while not await request.is_disconnected():
        await asyncio.sleep(1)
        try:
            await get_session(session_id)
        except HTTPException:
            yield "event: session.expired\ndata: {}\n\n"
            return
        updated = await read_snapshot(db, source)
        if updated.snapshotId != previous:
            previous = updated.snapshotId
            yield f"event: monitoring.snapshot\ndata: {updated.model_dump_json()}\n\n"
        else:
            yield ": keepalive\n\n"


@router.get("/events")
async def events(
    request: Request,
    session: CurrentSession,
    source: Source,
    db: Database,
    session_id: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> StreamingResponse:
    initial = await read_snapshot(db, source)
    return StreamingResponse(
        snapshot_events(request, db, source, session_id, initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
