"""Authenticated read-only references to event intervals in daily recordings."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.errors import MEDIA_ERROR_CODES, error_responses
from src.app.api.v1.media_control import CurrentSession
from src.app.db.session import get_db_session
from src.app.schemas.event_recording import EventRecordingPage, EventRecordingReplay
from src.app.services.event_recording_service import list_event_recordings, replay_event_recording


async def disable_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/api/v2/recordings/events",
    tags=["Event Recordings"],
    dependencies=[Depends(disable_cache)],
    responses=error_responses(401, 404, 422, 503, media=True),
)
Database = Annotated[AsyncSession, Depends(get_db_session)]
ContextSeconds = Annotated[int, Query(ge=0, le=300)]


@router.get("", response_model=EventRecordingPage)
async def list_events(
    session: CurrentSession,
    db: Database,
    pit_id: str | None = Query(default=None, min_length=1, max_length=50),
    category: Literal["all", "collection", "alert", "error"] = "all",
    from_time: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to_time: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    before_seconds: ContextSeconds = 30,
    after_seconds: ContextSeconds = 30,
    include_playback: bool = False,
) -> EventRecordingPage:
    return await list_event_recordings(
        db,
        pit_id,
        category,
        from_time,
        to_time,
        page,
        page_size,
        before_seconds,
        after_seconds,
        include_playback,
    )


@router.get(
    "/{event_id}",
    response_model=EventRecordingReplay,
    responses=error_responses(*MEDIA_ERROR_CODES, media=True),
)
async def replay_event(
    session: CurrentSession,
    db: Database,
    event_id: Annotated[
        str,
        Path(
            pattern=r"^(?:evt-auto-col-start|evt-alt|evt-col|evt-v2-transition|evt-err)-[1-9][0-9]{0,18}$"
        ),
    ],
    pit_id: str | None = Query(default=None, min_length=1, max_length=50),
    before_seconds: ContextSeconds = 30,
    after_seconds: ContextSeconds = 30,
) -> EventRecordingReplay:
    return await replay_event_recording(db, event_id, pit_id, before_seconds, after_seconds)
