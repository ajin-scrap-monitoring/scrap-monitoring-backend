"""Alert Engine with cooldown control and non-blocking notification dispatch."""

import asyncio
import logging
from datetime import UTC, datetime

from src.app.core.config import get_settings
from src.app.db.session import get_sessionmaker
from src.app.models.log import AlertLog

logger = logging.getLogger(__name__)


class AlertEngine:
    """Manages threshold alert triggers, cooldown timers, and asynchronous dispatch."""

    def __init__(self) -> None:
        # Tracks last alert timestamp per pit_id
        self._last_alert_time: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def handle_alert(
        self,
        pit_id: str,
        fill_ratio_percent: float,
        is_new_candidate: bool,
    ) -> None:
        """Process alert event asynchronously without blocking caller."""
        # Fire-and-forget background task
        asyncio.create_task(self._process_alert(pit_id, fill_ratio_percent, is_new_candidate))

    async def _process_alert(
        self,
        pit_id: str,
        fill_ratio_percent: float,
        is_new_candidate: bool,
    ) -> None:
        """Internal worker executing cooldown checks and logging."""
        settings = get_settings()
        now = datetime.now(UTC)
        cooldown_seconds = settings.ALERT_COOLDOWN_SECONDS

        elapsed = 0.0
        async with self._lock:
            last_time = self._last_alert_time.get(pit_id)
            in_cooldown = False

            if last_time:
                elapsed = (now - last_time).total_seconds()
                if elapsed < cooldown_seconds:
                    in_cooldown = True

            if in_cooldown:
                logger.debug(
                    "Alert suppressed for pit %s: in cooldown (elapsed %.1fs < %ds)",
                    pit_id,
                    elapsed,
                    cooldown_seconds,
                )
                return

            # Update last alert time
            self._last_alert_time[pit_id] = now

        # Record alert event in database
        message = (
            f"[CRITICAL ALERT] Scrap pit '{pit_id}' has reached critical fill ratio: "
            f"{fill_ratio_percent:.1f}%. Immediate collection required."
        )

        logger.warning(message)

        # Dispatch async DB write
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            try:
                alert_log = AlertLog(
                    pit_id=pit_id,
                    triggered_at=now,
                    fill_ratio_percent=fill_ratio_percent,
                    channel="LOG" if not settings.ALERT_SMTP_HOST else "EMAIL",
                    status="SENT",
                    message=message,
                )
                session.add(alert_log)
                await session.commit()
            except Exception as exc:
                logger.error("Failed to persist alert log to database: %s", exc)


# Global Alert Engine Singleton
alert_engine = AlertEngine()
