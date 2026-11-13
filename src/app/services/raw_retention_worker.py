"""Opt-in, single-instance raw cleanup with bounded commits and safe cancellation."""

import asyncio
import logging

from src.app.core.config import get_settings
from src.app.db.session import get_sessionmaker
from src.app.services.raw_retention import RetentionCursor, retention_batch

logger = logging.getLogger(__name__)


class RawRetentionWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if get_settings().EDGE_V2_RAW_RETENTION_ENABLED and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    async def _run(self) -> None:
        cursor = RetentionCursor()
        while True:
            settings = get_settings()
            if not settings.EDGE_V2_RAW_RETENTION_ENABLED:
                return
            try:
                async with get_sessionmaker()() as db:
                    result, next_cursor = await retention_batch(
                        db, cursor, apply=True, settings=settings
                    )
                    await db.commit()
                # Failed/cancelled transactions never advance the next batch cursor.
                cursor = next_cursor
                if result.candidates_examined:
                    logger.info(
                        "RAW_RETENTION_BATCH artifacts=%s measurements=%s clouds=%s skipped=%s",
                        result.artifact_bodies,
                        result.measurement_bodies,
                        result.state_clouds,
                        result.invalid_times_skipped,
                    )
                delay = 1.0
                if result.exhausted:
                    cursor = RetentionCursor()
                    delay = settings.EDGE_V2_RAW_RETENTION_INTERVAL_SECONDS
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # SQL errors may contain original bodies. Log the exception class only.
                logger.warning("RAW_RETENTION_FAILED code=%s", type(exc).__name__)
                await asyncio.sleep(60)


raw_retention_worker = RawRetentionWorker()
