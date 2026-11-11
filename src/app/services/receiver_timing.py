"""Optional, payload-free timings for authenticated v2 storage requests."""

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from time import perf_counter

from src.app.schemas.edge_v2 import Assessment, Envelope, Measurement

logger = logging.getLogger(__name__)


class ReceiverTiming:
    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self.started = perf_counter()
        self.validation_ms: float | None = None
        self.phases: dict[str, float] = {}
        self.metadata: dict[str, str] = {}

    def bind(self, payload: Envelope) -> None:
        """Only validated, authenticated requests may produce a diagnostic event."""
        self.validation_ms = (perf_counter() - self.started) * 1000
        self.metadata = {
            "message_sha256": hashlib.sha256(payload.message_id.encode()).hexdigest(),
            "source_sha256": hashlib.sha256(
                json.dumps([payload.site_id, payload.edge_id], separators=(",", ":")).encode()
            ).hexdigest(),
            "generated_at": payload.generated_at.isoformat(),
            "message_type": type(payload).__name__,
        }
        if isinstance(payload, Measurement):
            self.metadata["delivery_kind"] = payload.delivery_kind
        if isinstance(payload, Assessment):
            self.metadata["assessment_type"] = payload.assessment_type

    def emit(self, http_status: int, minimum_ms: float) -> None:
        total_ms = (perf_counter() - self.started) * 1000
        if not self.metadata or total_ms < minimum_ms:
            return
        event = {
            "event": "edge_v2_receiver_timing",
            **self.metadata,
            "route_started_at": self.started_at.isoformat(),
            "route_completed_at": datetime.now(UTC).isoformat(),
            "http_status": http_status,
            "total_ms": round(total_ms, 3),
            "validation_ms": round(self.validation_ms or 0, 3),
            "phases_ms": {key: round(value, 3) for key, value in self.phases.items()},
        }
        # Diagnostic output must never turn a committed receipt into a failed ACK.
        try:
            logger.info("%s", json.dumps(event, separators=(",", ":")))
        except Exception:
            pass


@contextmanager
def phase(timing: ReceiverTiming | None, name: str) -> Iterator[None]:
    if timing is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        timing.phases[name] = timing.phases.get(name, 0) + (perf_counter() - started) * 1000
