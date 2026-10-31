"""Authenticated read-only references to event intervals in daily recordings."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.daily_recordings import require_daily_csrf
from src.app.api.errors import MEDIA_ERROR_CODES, error_responses
from src.app.api.v1.media_control import CurrentSession
from src.app.db.session import get_db_session
from src.app.schemas.errors import HTTPErrorResponse, MediaErrorResponse
from src.app.schemas.event_export import EventExportRequest, EventExportResponse
from src.app.schemas.event_recording import EventRecordingPage, EventRecordingReplay
from src.app.services.event_export_service import read_export, request_export
from src.app.services.event_recording_service import list_event_recordings, replay_event_recording
from src.app.services.stable_event_pages import stable_event_page


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
ExportEventId = Annotated[
    str,
    Path(
        pattern=r"^(?:evt-auto-col-start|evt-alt|evt-col|evt-v2-transition|evt-err)-[1-9][0-9]{0,18}$"
    ),
]


@router.post(
    "/{event_id}/exports",
    response_model=EventExportResponse,
    dependencies=[Depends(require_daily_csrf)],
    responses={
        202: {"model": EventExportResponse},
        **error_responses(403, *MEDIA_ERROR_CODES, media=True),
        409: {"model": HTTPErrorResponse | MediaErrorResponse},
    },
)
async def prepare_event_export(
    session: CurrentSession, db: Database, event_id: ExportEventId, payload: EventExportRequest
) -> JSONResponse:
    """Only an explicit authenticated download request can create a range file."""
    code, item = await request_export(db, session.user.id, event_id, payload)
    return JSONResponse(
        status_code=code,
        content=item.model_dump(mode="json"),
        headers={"Location": item.statusPath, "Cache-Control": "no-store"},
    )


@router.get(
    "/{event_id}/exports/{request_id}",
    response_model=EventExportResponse,
    responses=error_responses(*MEDIA_ERROR_CODES, media=True),
)
async def get_event_export(
    session: CurrentSession, db: Database, event_id: ExportEventId, request_id: UUID
) -> EventExportResponse:
    return await read_export(db, session.user.id, event_id, request_id)


@router.get("", response_model=EventRecordingPage, responses=error_responses(409, 410))
async def list_events(
    request: Request,
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
    stable: bool = False,
    snapshot_token: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
) -> EventRecordingPage:
    if stable or snapshot_token is not None:
        return await stable_event_page(
            db,
            request.app.state.event_page_store,
            session.user.id,
            snapshot_token,
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
