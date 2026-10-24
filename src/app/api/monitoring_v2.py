"""Session-authenticated v2 dashboard snapshot and bounded-cadence SSE."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1.session import get_session
from src.app.db.session import get_db_session
from src.app.schemas.active_alerts_v2 import ActiveFillAlertPageV2
from src.app.schemas.collection_summary_v2 import CollectionSummaryV2
from src.app.schemas.fill_forecast import FillForecastV2
from src.app.schemas.load_history_v2 import HistoryInterval, LoadHistoryV2
from src.app.schemas.monitoring_v2 import (
    CollectionEventPage,
    LidarProfilePageV2,
    MonitoringSnapshotV2,
    MonitoringSource,
    StateEventPageV2,
    V2AlertPage,
)
from src.app.schemas.session import Session
from src.app.services.active_alerts_v2_service import active_fill_alerts
from src.app.services.collection_summary_v2_service import collection_summary
from src.app.services.edge_v2_history import alerts, collection_events, state_events
from src.app.services.fill_forecast_service import fill_forecast
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from src.app.services.session_service import SESSION_COOKIE_NAME

router = APIRouter(prefix="/api/v2/monitoring", tags=["Monitoring v2"])
Database = Annotated[AsyncSession, Depends(get_db_session)]
CurrentSession = Annotated[Session, Depends(get_session)]


def get_source(
    pit_id: str = Query(default="pit-01", min_length=1, max_length=128),
) -> MonitoringSource:
    return monitoring_source(pit_id)


Source = Annotated[MonitoringSource, Depends(get_source)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


@router.get("/active-alerts", response_model=ActiveFillAlertPageV2)
async def get_active_alerts(
    response: Response, session: CurrentSession, source: Source, db: Database
) -> ActiveFillAlertPageV2:
    response.headers["Cache-Control"] = "no-store"
    return await active_fill_alerts(db, source)


@router.get("/collection-summary", response_model=CollectionSummaryV2)
async def get_collection_summary(
    response: Response,
    session: CurrentSession,
    source: Source,
    db: Database,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> CollectionSummaryV2:
    response.headers["Cache-Control"] = "no-store"
    return await collection_summary(db, source, window_days)


@router.get("/fill-forecast", response_model=FillForecastV2)
async def get_fill_forecast(
    response: Response, session: CurrentSession, source: Source, db: Database
) -> FillForecastV2:
    response.headers["Cache-Control"] = "no-store"
    return await fill_forecast(db, source)


@router.get("/profiles", response_model=LidarProfilePageV2)
async def profiles(
    response: Response, session: CurrentSession, source: Source, db: Database
) -> LidarProfilePageV2:
    response.headers["Cache-Control"] = "no-store"
    return await lidar_profiles(db, source)


@router.get("/state-events", response_model=StateEventPageV2)
async def state_event_history(
    response: Response,
    session: CurrentSession,
    source: Source,
    db: Database,
    limit: Limit = 20,
    offset: Offset = 0,
) -> StateEventPageV2:
    response.headers["Cache-Control"] = "no-store"
    return await state_events(db, source, limit, offset)


@router.get("/collection-detections", response_model=StateEventPageV2)
async def collection_detection_history(
    response: Response,
    session: CurrentSession,
    source: Source,
    db: Database,
    limit: Limit = 20,
    offset: Offset = 0,
) -> StateEventPageV2:
    response.headers["Cache-Control"] = "no-store"
    return await state_events(db, source, limit, offset, collection_detections_only=True)


@router.get("/load-history", response_model=LoadHistoryV2)
async def get_load_history(
    response: Response,
    session: CurrentSession,
    source: Source,
    db: Database,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    bucket_seconds: Annotated[HistoryInterval | None, Query()] = None,
) -> LoadHistoryV2:
    response.headers["Cache-Control"] = "no-store"
    return await load_history(db, source, from_time, to_time, bucket_seconds)


@router.get("/alerts", response_model=V2AlertPage)
async def alert_history(
    response: Response,
    session: CurrentSession,
    source: Source,
    db: Database,
    limit: Limit = 20,
    offset: Offset = 0,
) -> V2AlertPage:
    response.headers["Cache-Control"] = "no-store"
    return await alerts(db, source, limit, offset, session.user.id)


@router.get("/collection-events", response_model=CollectionEventPage)
async def collection_history(
    response: Response,
    session: CurrentSession,
    source: Source,
    db: Database,
    limit: Limit = 20,
    offset: Offset = 0,
) -> CollectionEventPage:
    response.headers["Cache-Control"] = "no-store"
    return await collection_events(db, source, limit, offset)


@router.get("/collection-events/{assessment_id}/revisions", response_model=CollectionEventPage)
async def collection_revisions(
    response: Response,
    assessment_id: str,
    session: CurrentSession,
    source: Source,
    db: Database,
    limit: Limit = 20,
    offset: Offset = 0,
) -> CollectionEventPage:
    response.headers["Cache-Control"] = "no-store"
    return await collection_events(db, source, limit, offset, assessment_id)


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
            await get_session(db, session_id)
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
