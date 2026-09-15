"""History and operational event endpoints conforming to frontend proposal."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session
from src.app.schemas.history import EventPage, LoadHistory
from src.app.services.history_service import history_service

history_router = APIRouter(prefix="/history", tags=["History"])
events_router = APIRouter(prefix="/events", tags=["History"])


@history_router.get(
    "/load",
    response_model=LoadHistory,
    summary="Get representative load percentage history",
)
async def get_load_history(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    category: Literal["all", "collection", "alert", "error"] = Query(
        default="all", alias="category"
    ),
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> LoadHistory:
    """Query time-series representative load samples and event markers."""
    return await history_service.get_load_history(
        pit_id=pit_id,
        from_time=from_time,
        to_time=to_time,
        category=category,
        db=db,
    )


@events_router.get(
    "",
    response_model=EventPage,
    summary="List operational events",
)
async def list_operational_events(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    category: Literal["all", "collection", "alert", "error"] = Query(
        default="all", alias="category"
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, alias="pageSize", description="Page size"),
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> EventPage:
    """Query paginated operational events (collection, alert, error) ordered by time descending."""
    return await history_service.list_events(
        pit_id=pit_id,
        from_time=from_time,
        to_time=to_time,
        category=category,
        page=page,
        pageSize=page_size,
        db=db,
    )
