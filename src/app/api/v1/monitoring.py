"""Monitoring API endpoints conforming to frontend dashboard contract."""

import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session
from src.app.schemas.monitoring import MonitoringSnapshot
from src.app.services.broadcaster import broadcaster
from src.app.services.snapshot_service import snapshot_service

router = APIRouter(prefix="/monitoring", tags=["Monitoring"])


@router.get(
    "/snapshot",
    response_model=MonitoringSnapshot,
    summary="Get current monitoring dashboard snapshot",
)
async def get_monitoring_snapshot(
    response: Response,
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> MonitoringSnapshot:
    """Retrieve full dashboard snapshot conforming to proposal/openapi.yaml."""
    response.headers["Cache-Control"] = "no-store"
    return await snapshot_service.build_snapshot(pit_id, db)


@router.get(
    "/events",
    summary="Stream monitoring updates with Server-Sent Events",
    tags=["Realtime"],
)
async def stream_monitoring_events(
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Stream real-time monitoring snapshot events to dashboard via SSE."""

    async def event_generator() -> AsyncGenerator[str]:
        # 1. Send initial snapshot immediately upon connection
        initial_snapshot = await snapshot_service.build_snapshot(pit_id, db)
        payload = initial_snapshot.model_dump_json()
        yield f"event: monitoring.snapshot\ndata: {payload}\n\n"

        # 2. Register to broadcaster and stream subsequent updates as snapshots
        queue = await broadcaster.register_sse_queue()
        try:
            while True:
                # Wait for next state broadcast
                await queue.get()
                updated_snapshot = await snapshot_service.build_snapshot(pit_id, db)
                payload = updated_snapshot.model_dump_json()
                yield f"event: monitoring.snapshot\ndata: {payload}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            await broadcaster.unregister_sse_queue(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
