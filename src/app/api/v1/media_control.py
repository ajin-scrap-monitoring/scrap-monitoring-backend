"""Authenticated camera control API; Nginx delivers live/file bytes separately."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1.session import get_session
from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.schemas.media_service import (
    ClipDetail,
    ClipJobResponse,
    ClipPage,
    ClipReceipt,
    ClipRequest,
    ClipStatus,
    RecordingPolicy,
)
from src.app.schemas.session import Session
from src.app.services.media_client import MediaServiceError, media_client
from src.app.services.media_job_service import (
    apply_snapshot,
    enqueue_event_clip,
    job_snapshot,
    media_job_worker,
    process_job,
)
from src.app.services.session_service import SESSION_COOKIE_NAME

router = APIRouter(prefix="/media", tags=["Media Control"])
CurrentSession = Annotated[Session, Depends(get_session)]
Database = Annotated[AsyncSession, Depends(get_db_session)]
CameraId = Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")]
CameraPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")]


async def require_recording_session(
    scrap_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> None:
    """Keep legacy standalone demos compatible; configured media requires a session."""
    if get_settings().MEDIA_SERVICE_URL:
        await get_session(scrap_session)


async def require_collection_media_admin(
    scrap_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Recording-enabled collection confirmation uses the media mutation permissions."""
    if get_settings().MEDIA_SERVICE_URL:
        session = await get_session(scrap_session)
        await require_media_admin(session, csrf_token)


async def require_media_admin(
    session: CurrentSession,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Session:
    """Mutations require an administrator and the browser session's CSRF token."""
    if session.user.role != "administrator":
        raise HTTPException(403, "Administrator access required")
    if csrf_token is None or csrf_token != session.csrfToken:
        raise HTTPException(403, "Invalid or missing CSRF token")
    return session


async def media_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Return only safe codes; upstream authentication is not browser authentication."""
    assert isinstance(exc, MediaServiceError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": "Media service request failed"}},
    )


@router.get("/access", status_code=204, include_in_schema=False)
async def verify_media_access(session: CurrentSession) -> Response:
    """Nginx auth_request checks the same cookie used by the dashboard."""
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/cameras/{camera_id}/recording-policy", response_model=RecordingPolicy)
async def get_recording_policy(camera_id: CameraPath, session: CurrentSession) -> RecordingPolicy:
    return await media_client.get_policy(camera_id)


@router.put(
    "/cameras/{camera_id}/recording-policy",
    response_model=RecordingPolicy,
    dependencies=[Depends(require_media_admin)],
)
async def put_recording_policy(camera_id: CameraPath, policy: RecordingPolicy) -> RecordingPolicy:
    return await media_client.put_policy(camera_id, policy)


@router.post("/clips", dependencies=[Depends(require_media_admin)])
async def request_event_clip(payload: ClipRequest, db: Database) -> JSONResponse:
    """Request an existing backend event; retries cannot invent a second event ID."""
    settings = get_settings()
    if not settings.MEDIA_SERVICE_URL:
        raise MediaServiceError(503, "MEDIA_NOT_CONFIGURED")
    if payload.camera_id != settings.MEDIA_CAMERA_ID:
        raise HTTPException(404, "CAMERA_NOT_FOUND")
    async with media_job_worker.lock:
        job = await db.scalar(
            select(MediaClipJob).where(
                MediaClipJob.event_id == payload.event_id,
                MediaClipJob.camera_id == payload.camera_id,
            )
        )
        if job is None:
            prefix, _, identifier = payload.event_id.rpartition("-")
            if not identifier.isascii() or not identifier.isdecimal() or len(identifier) > 19:
                raise HTTPException(404, "EVENT_NOT_FOUND")
            event_number = int(identifier)
            if not 1 <= event_number <= 9223372036854775807 or str(event_number) != identifier:
                raise HTTPException(404, "EVENT_NOT_FOUND")
            event: AlertLog | CollectionLog | None = None
            if prefix == "evt-alt":
                event = await db.get(AlertLog, event_number)
                category = "alert"
                occurred_at = event.triggered_at if event else datetime.now(UTC)
            elif prefix == "evt-col":
                event = await db.get(CollectionLog, event_number)
                category = "collection"
                occurred_at = event.completed_at if event else datetime.now(UTC)
            else:
                raise HTTPException(404, "EVENT_NOT_FOUND")
            if event is None or event.pit_id != settings.MEDIA_PIT_ID:
                raise HTTPException(404, "EVENT_NOT_FOUND")
            job = await enqueue_event_clip(
                db, payload.event_id, event.pit_id, category, occurred_at
            )
            assert job is not None
            # Save identity before HTTP; a timeout must leave a recoverable request.
            await db.commit()
        code = await process_job(job, db)
        receipt = ClipReceipt.model_validate(job.snapshot)
        return JSONResponse(
            status_code=code,
            content=receipt.model_dump(mode="json"),
            headers={"Location": f"/api/v1/media/clips/{receipt.clip_id}"},
        )


@router.get("/clip-jobs", response_model=list[ClipJobResponse])
async def list_clip_jobs(
    session: CurrentSession,
    db: Database,
    event_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ClipJobResponse]:
    """Expose dispatch failures separately from remote encoding failures."""
    stmt = select(MediaClipJob).order_by(MediaClipJob.id.desc()).limit(limit)
    if event_id is not None:
        stmt = stmt.where(MediaClipJob.event_id == event_id)
    jobs = (await db.scalars(stmt)).all()
    return [
        ClipJobResponse(
            event_id=job.event_id,
            camera_id=job.camera_id,
            status=job.status,
            attempts=job.attempts,
            next_attempt_at=job.next_attempt_at,
            error_code=job.error_code,
            clip=job_snapshot(job),
        )
        for job in jobs
    ]


@router.get("/clips", response_model=ClipPage)
async def list_media_clips(
    session: CurrentSession,
    camera_id: CameraId | None = None,
    event_id: str | None = Query(default=None, min_length=1, max_length=128),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    status: ClipStatus | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=4096),
) -> ClipPage:
    params: dict[str, str | int] = {"limit": limit}
    for key, value in {
        "camera_id": camera_id,
        "event_id": event_id,
        "status": status,
        "cursor": cursor,
    }.items():
        if value is not None:
            params[key] = value
    for key, time_value in {"from": from_time, "to": to_time}.items():
        if time_value is not None:
            if time_value.tzinfo is None:
                raise HTTPException(422, "Time filters must include a timezone")
            params[key] = (
                time_value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
    if from_time and to_time and from_time >= to_time:
        raise HTTPException(422, "from must precede to")
    return await media_client.list_clips(params)


@router.get("/clips/{clip_id}", response_model=ClipDetail)
async def get_media_clip(clip_id: UUID, session: CurrentSession, db: Database) -> ClipDetail:
    async with media_job_worker.lock:
        detail = await media_client.get_clip(clip_id)
        job = await db.scalar(select(MediaClipJob).where(MediaClipJob.clip_id == str(clip_id)))
        if job:
            apply_snapshot(job, detail)
            await db.commit()
        return detail


@router.delete("/clips/{clip_id}", status_code=204, dependencies=[Depends(require_media_admin)])
async def delete_media_clip(clip_id: UUID, db: Database) -> Response:
    async with media_job_worker.lock:
        await media_client.delete_clip(clip_id)
        # Refresh rather than invent deleted_at; repeated DELETE can retain expired.
        job = await db.scalar(select(MediaClipJob).where(MediaClipJob.clip_id == str(clip_id)))
        if job:
            job.status = "refreshing"
            job.snapshot = None
            job.next_attempt_at = datetime.now(UTC)
            await db.commit()
        return Response(status_code=204)
