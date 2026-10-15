"""Single-worker operational projection, isolated from receiver ACKs and external I/O."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.db.session import get_sessionmaker
from src.app.models.edge_v2_operations import EdgeV2AlertLink, EdgeV2Operation
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.edge_v2_service import utc
from src.app.services.monitoring_v2_service import build_snapshot

logger = logging.getLogger(__name__)


async def process_source(db: AsyncSession, source: MonitoringSource, now: datetime) -> None:
    """Commit cursor, hysteresis and notification together; replay is a no-op."""
    row = await db.scalar(
        select(EdgeV2Operation)
        .where(EdgeV2Operation.pit_id == source.pit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    snapshot = await build_snapshot(db, source, now)
    assessment = snapshot.assessment
    fill = snapshot.summary.loadPercent
    if snapshot.dataState != "FRESH" or assessment is None or fill is None:
        if row and row.critical_since is not None:
            row.critical_since = None
            await db.commit()
        return
    observed_at = assessment.observed_to
    assert observed_at is not None
    assert assessment.fill_estimate is not None
    # Display rounding must not move a value across an alarm/recovery threshold.
    if snapshot.summary.filterState == "DISABLED":
        fill = assessment.fill_estimate.ratio * 100.0
    same_source = row is not None and (row.site_id, row.edge_id) == (source.site_id, source.edge_id)
    if (
        row
        and same_source
        and (
            assessment.generated_at <= utc(row.generated_at) or observed_at <= utc(row.observed_at)
        )
    ):
        return
    warn = snapshot.summary.warningThresholdPercent
    critical = snapshot.summary.collectionThresholdPercent
    recovery = critical - 5.0
    if not 0 <= warn <= recovery < critical <= 100:
        # Bad settings never create a notification or reset an existing alarm.
        logger.error("Invalid v2 operational thresholds; source evaluation skipped")
        return
    previous = row.state if row and same_source else "NORMAL"
    pending = None
    if snapshot.summary.filterState == "READY" and previous != "CRITICAL":
        # The receiver observes every distinct input even when a worker sweep is delayed.
        # Its timer resets on gaps/invalidity/source changes, not on wall-clock polling.
        filtered = await db.get(EdgeV2Filter, (source.site_id, source.edge_id))
        if (
            filtered
            and filtered.payload.get("message_id") == assessment.message_id
            and filtered.payload.get("threshold") == critical
        ):
            stamp = filtered.payload.get("above_since")
            pending = datetime.fromisoformat(stamp) if stamp else None
        if fill >= critical:
            pending = pending or observed_at
            confirmed = observed_at - pending >= timedelta(
                seconds=get_settings().EDGE_V2_CRITICAL_CONFIRM_SECONDS
            )
        else:
            pending = None
            confirmed = False
    else:
        pending = None
        confirmed = fill >= critical
    if previous == "CRITICAL" and fill > recovery:
        current = "CRITICAL"
    elif fill >= critical and confirmed:
        current = "CRITICAL"
    elif fill >= warn:
        current = "WARNING"
    else:
        current = "NORMAL"
    if row is None:
        row = EdgeV2Operation(pit_id=source.pit_id)
        db.add(row)
    row.site_id, row.edge_id = source.site_id, source.edge_id
    row.state = current
    row.message_id = assessment.message_id
    row.generated_at = assessment.generated_at
    row.observed_at = observed_at
    row.critical_since = pending
    if snapshot.summary.filterState == "READY" and previous != current:
        db.add(
            EdgeV2Transition(
                site_id=source.site_id,
                edge_id=source.edge_id,
                occurred_at=observed_at,
                kind="FILL_STATE_CHANGED",
                payload={
                    "previous": previous,
                    "current": current,
                    "valuePercent": fill,
                    "assessmentId": assessment.assessment_id,
                    "messageId": assessment.message_id,
                },
            )
        )
    cooldown = timedelta(seconds=get_settings().ALERT_COOLDOWN_SECONDS)
    if current == "CRITICAL" and (
        row.last_alert_at is None or now - utc(row.last_alert_at) >= cooldown
    ):
        alert = AlertLog(
            pit_id=source.pit_id,
            triggered_at=now,
            fill_ratio_percent=fill,
            channel="LOG",
            status="RECORDED",
            event_type="collection_required",
            severity="error",
            message=f"스크랩 적재율 {fill:.1f}%: 수거 필요 (라이다 v2 판정)",
        )
        db.add(alert)
        await db.flush()
        db.add(
            EdgeV2AlertLink(
                alert_id=alert.id,
                pit_id=source.pit_id,
                site_id=source.site_id,
                edge_id=source.edge_id,
                assessment_id=assessment.assessment_id,
                message_id=assessment.message_id,
                observed_at=observed_at,
            )
        )
        row.last_alert_at = now
        row.last_alert_id = alert.id
    await db.commit()


class EdgeV2OperationsWorker:
    """One process/worker, matching the existing media worker's process-local lock."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if get_settings().EDGE_V2_OPERATIONS_ENABLED and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self, db: AsyncSession, now: datetime | None = None) -> None:
        if not get_settings().EDGE_V2_OPERATIONS_ENABLED:
            return
        async with self._lock:
            for source in get_settings().EDGE_V2_MONITORING_SOURCES:
                try:
                    await process_source(db, source, now or datetime.now(UTC))
                finally:
                    await db.rollback()

    async def _run(self) -> None:
        while True:
            try:
                async with get_sessionmaker()() as db:
                    await self.run_once(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Avoid emitting SQL parameters, raw data or credentials in failures.
                logger.error("V2 operational processing failed; retrying next sweep")
            await asyncio.sleep(1)


edge_v2_operations_worker = EdgeV2OperationsWorker()
