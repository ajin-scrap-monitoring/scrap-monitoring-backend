"""Read existing events and daily archive metadata without creating media jobs."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.schemas.daily_recording import DailyPage, DailyRecording
from src.app.schemas.event_recording import (
    EventRecording,
    EventRecordingPage,
    EventRecordingReplay,
    ReplayPart,
    UnavailableInterval,
)
from src.app.schemas.history import PageMetadata
from src.app.services.daily_recording_service import daily_recording_service
from src.app.services.edge_v2_service import utc
from src.app.services.event_recording_queries import (
    active_collection,
    event_table,
    playback_scope,
    reference,
    shifted,
)
from src.app.services.media_client import MediaServiceError

KST = ZoneInfo("Asia/Seoul")
MAX_DATES = 7
MAX_PARTS = 2000


def _context(before: int, after: int) -> None:
    if not (0 <= before <= 300 and 0 <= after <= 300 and before + after > 0):
        raise HTTPException(422, "Event context must span 1 to 600 seconds")


async def list_event_recordings(
    db: AsyncSession,
    pit_id: str | None,
    category: str,
    from_time: datetime | None,
    to_time: datetime | None,
    page: int,
    page_size: int,
    before: int,
    after: int,
    include_playback: bool = False,
) -> EventRecordingPage:
    _context(before, after)
    now = datetime.now(UTC)
    upper = utc(to_time) if to_time else now
    lower = utc(from_time) if from_time else shifted(upper, -7 * 86400)
    if lower >= upper or upper - lower > timedelta(days=90):
        raise HTTPException(422, "Use an increasing range of at most 90 days")
    pit, source = playback_scope(pit_id)
    active = await active_collection(db, source, now)
    table = event_table(db, pit, source, category, lower, upper, now, before, after, active)
    total = await db.scalar(select(func.count()).select_from(table)) or 0
    offset = (page - 1) * page_size
    # Count first: absent pages need no row query or potentially overflowing DB OFFSET.
    rows = (
        (
            (
                await db.execute(
                    select(table)
                    .order_by(table.c.at.desc(), table.c.event_id.desc())
                    .offset(offset)
                    .limit(page_size)
                )
            )
            .mappings()
            .all()
        )
        if offset < total
        else []
    )
    items = [reference(row, pit, before, after, active) for row in rows]
    if include_playback and items:
        from src.app.services.event_playback_summary import add_playback_summaries

        await add_playback_summaries(items, now)
    return EventRecordingPage(
        items=items,
        page=PageMetadata(
            page=page,
            pageSize=page_size,
            totalItems=total,
            totalPages=(total + page_size - 1) // page_size,
        ),
        from_=lower,
        to=upper,
        asOf=now,
    )


def _remaining(
    start: datetime, end: datetime, intervals: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    result = []
    cursor = start
    for left, right in sorted(intervals):
        left, right = max(left, start), min(right, end)
        if left > cursor:
            result.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        result.append((cursor, end))
    return result


def _duration(intervals: list[tuple[datetime, datetime]]) -> float:
    total = 0.0
    previous = None
    for left, right in sorted(intervals):
        if previous is not None:
            left = max(left, previous)
        if right > left:
            total += (right - left).total_seconds()
        previous = max(previous, right) if previous else right
    return total


async def _archive_days(
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    fetch_page: Callable[[dict[str, str | int]], Awaitable[DailyPage]] | None = None,
) -> list[DailyRecording]:
    end = min(end, now)
    if end <= start:
        return []
    first = start.astimezone(KST).date()
    last = (end - timedelta(microseconds=1)).astimezone(KST).date()
    if (last - first).days + 1 > MAX_DATES:
        raise HTTPException(422, "An event playback window may span at most seven Korean dates")
    params: dict[str, str | int] = {
        "from_date": first.isoformat(),
        "to_date": (last + timedelta(days=1)).isoformat(),
        "limit": 100,
    }
    items: list[DailyRecording] = []
    seen_ids, seen_dates, cursors = set(), set(), set()
    for _ in range(8):
        page = await (fetch_page or daily_recording_service.list)(params)
        for item in page.items:
            if (
                item.recording_id in seen_ids
                or item.recording_date in seen_dates
                or not first <= item.recording_date <= last
            ):
                raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
            seen_ids.add(item.recording_id)
            seen_dates.add(item.recording_date)
            items.append(item)
        if page.next_cursor is None:
            return items
        if page.next_cursor in cursors:
            raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
        cursors.add(page.next_cursor)
        params["cursor"] = page.next_cursor
    raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")


def _resolve_day(
    result: EventRecordingReplay,
    day: DailyRecording | None,
    start: datetime,
    end: datetime,
    now: datetime,
) -> list[tuple[datetime, datetime]]:
    covered: list[tuple[datetime, datetime]] = []
    confirmed = min(end, now)

    def missing(left: datetime, right: datetime, kind: str, reason: str) -> None:
        if right >= left:
            result.unavailableIntervals.append(
                UnavailableInterval(fromAt=left, toAt=right, kind=kind, reasonCode=reason)
            )

    if end > now:
        missing(max(start, now), end, "FUTURE", "FUTURE_INTERVAL")
    if confirmed <= start:
        return covered
    if day is None:
        missing(start, confirmed, "MISSING", "DAILY_RECORDING_NOT_FOUND")
        return covered
    if day.status in ("deleted", "expired") or (
        day.expires_at is not None and day.expires_at <= now
    ):
        missing(start, confirmed, "EXPIRED", "DAILY_RECORDING_EXPIRED")
        return covered
    if day.error is not None:
        result.reasonCodes.append(day.error.code)
    gaps = []
    for gap in day.gaps:
        left, right = max(start, gap.start_at), min(confirmed, gap.end_at)
        if left < right or (
            left == right and start <= gap.start_at < confirmed and gap.start_at == gap.end_at
        ):
            missing(left, right, "GAP", gap.reason_code)
            gaps.append((left, right))
    available_to = min(
        confirmed, day.coverage_as_of_at, day.playable_until_at or day.window_start_at
    )
    playable = (
        day.playback_state == "ready"
        and day.playback_path is not None
        and day.playable_until_at is not None
    )
    if playable:
        for interval in day.available_intervals:
            if interval.start_at == interval.end_at:
                point = interval.start_at
                if start <= point < confirmed and point <= available_to:
                    result.parts.append(
                        ReplayPart(
                            recordingId=day.recording_id,
                            recordingDate=day.recording_date,
                            playbackPath=day.playback_path,
                            fromAt=point,
                            toAt=point,
                            startSeconds=interval.playback_start_seconds,
                            endSeconds=interval.playback_end_seconds,
                            mapping="POINT_TIMESTAMP",
                        )
                    )
                continue
            left, right = max(start, interval.start_at), min(available_to, interval.end_at)
            if left >= right:
                continue
            if any(a < right and b > left for a, b in gaps):
                result.reasonCodes.append("COVERAGE_METADATA_CONFLICT")
                continue
            wall_seconds = (interval.end_at - interval.start_at).total_seconds()
            playback_seconds = interval.playback_end_seconds - interval.playback_start_seconds
            begin = (
                interval.playback_start_seconds
                + playback_seconds * (left - interval.start_at).total_seconds() / wall_seconds
            )
            finish = (
                interval.playback_start_seconds
                + playback_seconds * (right - interval.start_at).total_seconds() / wall_seconds
            )
            if finish <= begin:
                result.reasonCodes.append("PLAYBACK_MAPPING_PRECISION")
                continue
            result.parts.append(
                ReplayPart(
                    recordingId=day.recording_id,
                    recordingDate=day.recording_date,
                    playbackPath=day.playback_path,
                    fromAt=left,
                    toAt=right,
                    startSeconds=begin,
                    endSeconds=finish,
                )
            )
            covered.append((left, right))
    for left, right in _remaining(start, confirmed, covered + gaps):
        reason = (
            "COVERAGE_PENDING"
            if left >= day.coverage_as_of_at
            else "PLAYBACK_NOT_READY"
            if not playable
            else "COVERAGE_UNCONFIRMED"
        )
        missing(left, right, "UNCONFIRMED", reason)
    return covered


async def replay_event_recording(
    db: AsyncSession, event_id: str, pit_id: str | None, before: int, after: int
) -> EventRecordingReplay:
    _context(before, after)
    now = datetime.now(UTC)
    pit, source = playback_scope(pit_id)
    active = await active_collection(db, source, now)
    table = event_table(db, pit, source, "all", None, None, now, before, after, active, event_id)
    row = (await db.execute(select(table))).mappings().first()
    if row is None:
        raise HTTPException(404, "EVENT_NOT_FOUND")
    event = reference(row, pit, before, after, active)
    days = []
    if event.windowState != "UNKNOWN" and (event.windowTo or now) > event.windowFrom:
        days = await _archive_days(event.windowFrom, event.windowTo or now, now)
    return resolve_event_replay(event, now, days)


def resolve_event_replay(
    event: EventRecording, now: datetime, days: list[DailyRecording]
) -> EventRecordingReplay:
    """Use the same coverage and zero-based timeline rules for detail and list summaries."""
    result = EventRecordingReplay(event=event, state="UNAVAILABLE", asOf=now)
    if event.windowState == "UNKNOWN":
        result.reasonCodes = event.reasonCodes
        return result
    end = event.windowTo or now
    start = event.windowFrom
    result.effectiveTo, result.requestedSeconds = end, (end - start).total_seconds()
    if end <= start:
        result.reasonCodes = ["EVENT_WINDOW_EMPTY"]
        return result
    by_date = {day.recording_date: day for day in days}
    covered = []
    current = start
    while current < end:
        day_date = current.astimezone(KST).date()
        next_midnight = datetime.combine(day_date + timedelta(days=1), time(), KST).astimezone(UTC)
        until = min(end, next_midnight)
        covered.extend(_resolve_day(result, by_date.get(day_date), current, until, now))
        if len(result.parts) + len(result.unavailableIntervals) > MAX_PARTS:
            result.parts = []
            result.unavailableIntervals = []
            result.reasonCodes = ["EVENT_REPLAY_SEGMENT_LIMIT"]
            return result
        current = until
    result.parts.sort(key=lambda part: (part.fromAt, part.recordingDate, part.startSeconds))
    result.coveredSeconds = _duration(covered)
    for part in result.parts:
        part.relativeStartSeconds = result.playbackSeconds
        result.playbackSeconds += part.endSeconds - part.startSeconds
        part.relativeEndSeconds = result.playbackSeconds
    complete = not _remaining(start, end, covered) and not result.unavailableIntervals
    if complete and event.windowState != "OPEN":
        result.state = "AVAILABLE"
    elif result.parts:
        result.state = "PARTIAL"
    elif result.unavailableIntervals and all(
        i.kind == "FUTURE" or i.reasonCode in ("COVERAGE_PENDING", "PLAYBACK_NOT_READY")
        for i in result.unavailableIntervals
    ):
        result.state = "PROCESSING"
    if event.windowState == "OPEN":
        result.reasonCodes.append("COLLECTION_IN_PROGRESS")
    result.reasonCodes = list(
        dict.fromkeys(result.reasonCodes + [i.reasonCode for i in result.unavailableIntervals])
    )
    return result
