"""JSON-only camera v2 access and safe public video paths."""

from uuid import UUID

from src.app.core.config import get_settings
from src.app.schemas.daily_recording import (
    DailyAvailableInterval,
    DailyGap,
    DailyIntervalPage,
    DailyMetadataPage,
    DailyPage,
    DailyPolicy,
    DailyRecording,
    DailyRecordingMetadata,
    DailyRecordingState,
    DailyRecordingSummary,
    DailySummaryPage,
)
from src.app.schemas.media_info import DailyMediaInfo
from src.app.services.media_client import MediaClient, MediaServiceError, media_client


def public_recording[Recording: DailyRecordingState](recording: Recording) -> Recording:
    """Rewrite validated relative paths; never expose upstream URLs or error text."""
    if recording.camera_id != get_settings().MEDIA_CAMERA_ID:
        raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
    remote = f"/api/v2/daily-recordings/{recording.recording_id}"
    if (
        recording.playback_path not in (None, remote + "/hls/index.m3u8")
        or recording.download_path not in (None, remote + "/content")
        or (recording.playback_path is not None and recording.playback_state != "ready")
        or (recording.download_path is not None and recording.export_state != "ready")
    ):
        raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
    prefix = f"/media/daily-recordings/{recording.recording_id}"
    return recording.model_copy(
        update={
            "playback_path": prefix + "/hls/index.m3u8" if recording.playback_path else None,
            "download_path": prefix + "/content?download=true" if recording.download_path else None,
            "error": recording.error.model_copy(update={"message": "Media processing failed"})
            if recording.error
            else None,
            "cleanup_error": recording.cleanup_error.model_copy(
                update={"message": "Recording cleanup failed"}
            )
            if recording.cleanup_error
            else None,
        }
    )


class DailyRecordingService:
    def __init__(self, client: MediaClient) -> None:
        self.client = client

    async def policy(self, update: DailyPolicy | None = None) -> DailyPolicy:
        response = await self.client._request(
            "PUT" if update else "GET",
            f"cameras/{get_settings().MEDIA_CAMERA_ID}/daily-recording-policy",
            (200,),
            update,
            daily=True,
        )
        return (
            self.client._parse_policy_update(response, DailyPolicy)
            if update is not None
            else self.client._parse(response, DailyPolicy)
        )

    async def list(self, params: dict[str, str | int]) -> DailyPage:
        response = await self.client._request(
            "GET",
            f"cameras/{get_settings().MEDIA_CAMERA_ID}/daily-recordings",
            (200,),
            params=params,
            daily=True,
        )
        page = self.client._parse(response, DailyPage)
        return page.model_copy(update={"items": [public_recording(item) for item in page.items]})

    async def summary(self, params: dict[str, str | int]) -> DailySummaryPage:
        """Summarize only after full upstream validation and public-path sanitization."""
        page = await self.list(params)
        return DailySummaryPage(
            items=[
                DailyRecordingSummary.model_validate(
                    dict(
                        item,
                        gap_count=len(item.gaps),
                        available_interval_count=len(item.available_intervals),
                    )
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def detail(self, recording_id: UUID) -> DailyRecording:
        response = await self.client._request(
            "GET",
            f"daily-recordings/{recording_id}",
            (200,),
            daily=True,
        )
        item = self.client._parse(response, DailyRecording)
        if item.recording_id != recording_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return public_recording(item)

    async def metadata_list(self, params: dict[str, str | int]) -> DailyMetadataPage:
        """Read the upstream summary directly, without fetching either evidence array."""
        response = await self.client._request(
            "GET",
            f"cameras/{get_settings().MEDIA_CAMERA_ID}/daily-recordings/summary",
            (200,),
            params=params,
            daily=True,
        )
        page = self.client._parse(response, DailyMetadataPage)
        if len(page.items) > int(params["limit"]):
            raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
        return page.model_copy(update={"items": [public_recording(item) for item in page.items]})

    async def metadata(self, recording_id: UUID) -> DailyRecordingMetadata:
        response = await self.client._request(
            "GET", f"daily-recordings/{recording_id}/summary", (200,), daily=True
        )
        item = self.client._parse(response, DailyRecordingMetadata)
        if item.recording_id != recording_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return public_recording(item)

    async def interval_page[Interval: DailyGap | DailyAvailableInterval](
        self,
        recording_id: UUID,
        kind: str,
        page_type: type[DailyIntervalPage[Interval]],
        params: dict[str, str | int],
    ) -> DailyIntervalPage[Interval]:
        # Establish camera ownership without the legacy full-detail read. Reading
        # metadata and a page can race an upstream revision; never mix those states.
        metadata = await self.metadata(recording_id)
        response = await self.client._request(
            "GET", f"daily-recordings/{recording_id}/{kind}", (200,), params=params, daily=True
        )
        page = self.client._parse(response, page_type)
        if page.recording_id != recording_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        if page.revision != metadata.revision:
            raise MediaServiceError(409, "DAILY_SNAPSHOT_CHANGED")
        if len(page.items) > int(params["limit"]):
            raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
        previous = metadata.window_start_at
        for interval in page.items:
            if interval.start_at < previous or interval.end_at > metadata.coverage_as_of_at:
                raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
            previous = interval.end_at
        return page

    async def export(self, recording_id: UUID) -> tuple[int, DailyRecordingMetadata]:
        # Ownership must be checked without reading the large evidence arrays.
        # The POST still performs exportability and actual MP4-file checks upstream.
        await self.metadata(recording_id)
        response = await self.client._request(
            "POST",
            f"daily-recordings/{recording_id}/exports/summary",
            (200, 202),
            daily=True,
        )
        item = self.client._parse(response, DailyRecordingMetadata)
        if item.recording_id != recording_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        if response.status_code != (200 if item.export_state == "ready" else 202):
            raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
        # Do not silently accept a full-history response from the lightweight POST.
        if {"gaps", "available_intervals", "segments"}.intersection(response.json()):
            raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
        return response.status_code, public_recording(item)

    async def media_info(self, recording_id: UUID) -> DailyMediaInfo:
        item = await self.metadata(recording_id)
        if item.status in ("deleted", "expired"):
            raise MediaServiceError(410, "DAILY_RECORDING_" + item.status.upper())
        info = await self.client.metadata(
            f"daily-recordings/{recording_id}/media-info", DailyMediaInfo, daily=True
        )
        if info.recording_id != recording_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return info

    async def delete(self, recording_id: UUID) -> None:
        # Check camera ownership before the destructive request without loading history.
        await self.metadata(recording_id)
        await self.client._request(
            "DELETE",
            f"daily-recordings/{recording_id}",
            (204,),
            daily=True,
        )


daily_recording_service = DailyRecordingService(media_client)
