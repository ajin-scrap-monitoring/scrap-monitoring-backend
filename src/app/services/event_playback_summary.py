"""Bounded, request-local archive sharing for opt-in event list summaries."""

import asyncio
from datetime import UTC, date, datetime, time, timedelta

from src.app.schemas.daily_recording import DailyPage, DailyRecording
from src.app.schemas.event_recording import EventPlaybackSummary, EventRecording
from src.app.services.daily_recording_service import daily_recording_service
from src.app.services.event_recording_service import (
    KST,
    MAX_DATES,
    _archive_days,
    resolve_event_replay,
)
from src.app.services.media_client import MediaServiceError

MAX_REQUESTS = 8
MAX_INTERVALS = 20_000
MAX_RESOLUTION_WORK = 200_000
SUMMARY_TIMEOUT_SECONDS = 10


class SummaryLimit(Exception):
    pass


def _unknown(event: EventRecording, reason: str) -> None:
    event.playback = EventPlaybackSummary(state="UNKNOWN", reasonCodes=[reason])


def _dates(event: EventRecording, now: datetime) -> list[date]:
    end = min(event.windowTo or now, now)
    if event.windowState == "UNKNOWN" or end <= event.windowFrom:
        return []
    first = event.windowFrom.astimezone(KST).date()
    last = (end - timedelta(microseconds=1)).astimezone(KST).date()
    count = (last - first).days + 1
    if count > MAX_DATES:
        raise SummaryLimit
    return [first + timedelta(days=n) for n in range(count)]


async def add_playback_summaries(items: list[EventRecording], now: datetime) -> None:
    """Share date metadata, never perform a detail request or create a clip job."""
    selected: set[date] = set()
    windows: dict[str, list[date]] = {}
    for event in items:
        try:
            dates = _dates(event, now)
            if len(selected.union(dates)) > MAX_DATES:
                raise SummaryLimit
        except SummaryLimit:
            _unknown(event, "PLAYBACK_SUMMARY_DATE_LIMIT")
            continue
        windows[event.eventId] = dates
        selected.update(dates)

    requests = intervals = 0

    async def fetch(params: dict[str, str | int]) -> DailyPage:
        nonlocal requests, intervals
        if requests >= MAX_REQUESTS:
            raise SummaryLimit
        requests += 1
        page = await daily_recording_service.list(params)
        intervals += sum(len(day.available_intervals) + len(day.gaps) for day in page.items)
        if intervals > MAX_INTERVALS:
            raise SummaryLimit
        return page

    # Consecutive dates use one query range. Sparse pages never read the intervening days.
    groups: list[list[date]] = []
    for day in sorted(selected):
        if not groups or day != groups[-1][-1] + timedelta(days=1):
            groups.append([])
        groups[-1].append(day)
    metadata: dict[date, DailyRecording] = {}
    errors: dict[date, str] = {}
    confirmed: set[date] = set()
    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
            for group in groups:
                start = datetime.combine(group[0], time(), KST).astimezone(UTC)
                end = datetime.combine(group[-1] + timedelta(days=1), time(), KST).astimezone(UTC)
                try:
                    days = await _archive_days(start, end, now, fetch_page=fetch)
                except MediaServiceError as exc:
                    errors.update(dict.fromkeys(group, exc.code))
                else:
                    metadata.update({day.recording_date: day for day in days})
                    confirmed.update(group)
    except (TimeoutError, SummaryLimit) as exc:
        reason = (
            "PLAYBACK_SUMMARY_TIMEOUT"
            if isinstance(exc, TimeoutError)
            else "PLAYBACK_SUMMARY_LIMIT"
        )
        # Discard partial pages in the interrupted group: absence has not been confirmed.
        for day in selected:
            if day not in confirmed and day not in errors:
                errors[day] = reason

    work = 0
    for event in items:
        if event.playback is not None:
            continue
        dates = windows[event.eventId]
        error = next((errors[day] for day in dates if day in errors), None)
        if error:
            _unknown(event, error)
            continue
        days = [metadata[day] for day in dates if day in metadata]
        # Bound scanning across the page, then compare only intervals that can
        # overlap this event. Unrelated gaps elsewhere in a day must not cause
        # quadratic work for every short event in that archive.
        scan = sum(len(day.available_intervals) + len(day.gaps) for day in days)
        if work + scan > MAX_RESOLUTION_WORK:
            _unknown(event, "PLAYBACK_SUMMARY_WORK_LIMIT")
            continue
        work += scan
        start, end = event.windowFrom, min(event.windowTo or now, now)
        days = [
            day.model_copy(
                update={
                    "available_intervals": [
                        span
                        for span in day.available_intervals
                        if span.start_at < end and span.end_at >= start
                    ],
                    "gaps": [gap for gap in day.gaps if gap.start_at < end and gap.end_at >= start],
                }
            )
            for day in days
        ]
        cost = sum((len(day.available_intervals) + 1) * (len(day.gaps) + 1) for day in days)
        if work + cost > MAX_RESOLUTION_WORK:
            _unknown(event, "PLAYBACK_SUMMARY_WORK_LIMIT")
            continue
        work += cost
        replay = resolve_event_replay(event, now, days)
        if "EVENT_REPLAY_SEGMENT_LIMIT" in replay.reasonCodes:
            _unknown(event, "EVENT_REPLAY_SEGMENT_LIMIT")
        else:
            event.playback = EventPlaybackSummary(
                state=replay.state,
                playbackSeconds=replay.playbackSeconds,
                reasonCodes=replay.reasonCodes,
            )
