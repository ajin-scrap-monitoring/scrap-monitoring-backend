"""Remove only reconstructible receipt metadata; keep every identity, hash and timestamp."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message

MINIMAL_KEYS = {"schema_version", "assessment_type", "retention"}
LEGACY_KEYS = MINIMAL_KEYS | {
    "site_id",
    "edge_id",
    "message_id",
    "message_type",
    "config_revision",
    "generated_at",
}


def minimal_receipt(schema_version: str, assessment_type: str | None) -> dict[str, Any]:
    """Schema/subtype and retention have no equivalent columns; retain them explicitly."""
    return {
        "schema_version": schema_version,
        "assessment_type": assessment_type,
        "retention": "LATEST_ONLY",
    }


def reducible_receipt(row: EdgeV2Message) -> dict[str, Any] | None:
    """Fail closed on unknown fields, mismatched columns, EVENTs or original bodies.

    The full original compact JSON can be reconstructed from these columns and
    the remaining three fields. No digest, composite key or receipt time is moved.
    """
    data = row.payload
    if (
        not row.compact
        or not isinstance(data, dict)
        or set(data) != LEGACY_KEYS
        or data.get("retention") != "LATEST_ONLY"
        or data.get("schema_version") != "2.0"
        or row.message_type not in {"lidar_measurement", "edge_assessment", "edge_status"}
        or data.get("assessment_type")
        != ("STATE" if row.message_type == "edge_assessment" else None)
    ):
        return None
    for key in ("site_id", "edge_id", "message_id", "message_type", "config_revision"):
        if data[key] != getattr(row, key):
            return None
    try:
        generated = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    stored = row.generated_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)  # SQLite stores the existing UTC column without tzinfo.
    if generated.tzinfo is None or generated.utcoffset() is None or generated != stored:
        return None
    return minimal_receipt(data["schema_version"], data["assessment_type"])


@dataclass
class ReceiptCompactionBatch:
    dry_run: bool
    after_id: int
    next_after_id: int
    scanned: int = 0
    eligible: int = 0
    changed: int = 0
    serialized_json_bytes_saved: int = 0
    exhausted: bool = False


async def compact_receipts(
    db: AsyncSession, *, after_id: int = 0, batch_size: int = 100, apply: bool = False
) -> ReceiptCompactionBatch:
    """Bounded primary-key scan; caller commits. No delete, DDL or external I/O.

    Apply locks selected rows, sharing the original expiration worker's row locks.
    Locked rows may be skipped and are reconsidered by a later sweep from zero.
    Exhaustion describes this unlocked scan, never all possible future candidates.
    """
    if not 0 <= after_id <= 2**63 - 1 or not 1 <= batch_size <= 1000:
        raise ValueError("Invalid receipt compaction bounds")
    if apply and not get_settings().EDGE_V2_COMPACT_STORAGE_ENABLED:
        raise ValueError("Receipt compaction requires compact storage")
    # Page IDs first: a page of original scans/EVENTs must never load their large
    # bodies or scan past an unbounded number of noncompact rows to fill a batch.
    ids = list(
        await db.scalars(
            select(EdgeV2Message.id)
            .where(EdgeV2Message.id > after_id)
            .order_by(EdgeV2Message.id)
            .limit(batch_size)
        )
    )
    result = ReceiptCompactionBatch(
        dry_run=not apply,
        after_id=after_id,
        next_after_id=ids[-1] if ids else after_id,
        scanned=len(ids),
        exhausted=len(ids) < batch_size,
    )
    if not ids:
        return result
    query = (
        select(EdgeV2Message)
        .where(
            EdgeV2Message.id.in_(ids),
            EdgeV2Message.compact.is_(True),
            EdgeV2Message.payload["retention"].as_string() == "LATEST_ONLY",
            func.length(cast(EdgeV2Message.payload, String)) <= 16384,
        )
        .order_by(EdgeV2Message.id)
    )
    if apply:
        query = query.with_for_update(skip_locked=True)
    query = query.execution_options(populate_existing=True)
    rows = list(await db.scalars(query))
    for row in rows:
        reduced = reducible_receipt(row)
        if reduced is None:
            continue
        result.eligible += 1
        result.serialized_json_bytes_saved += len(json.dumps(row.payload).encode()) - len(
            json.dumps(reduced).encode()
        )
        if apply:
            row.payload = reduced
            result.changed += 1
    return result
