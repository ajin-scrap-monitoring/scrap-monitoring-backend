"""Edge device management and heartbeat service."""

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge import EdgeHeartbeatModel
from src.app.schemas.edge import EdgeHeartbeatRequest, EdgeHeartbeatResponse

logger = logging.getLogger(__name__)


class EdgeService:
    """Handles Edge Platform heartbeat recording and health tracking."""

    async def record_heartbeat(
        self,
        payload: EdgeHeartbeatRequest,
        db: AsyncSession,
    ) -> EdgeHeartbeatResponse:
        """Record or update latest edge device heartbeat snapshot."""
        now = datetime.now(UTC)

        stmt = select(EdgeHeartbeatModel).where(EdgeHeartbeatModel.edge_id == payload.edge_id)
        res = await db.execute(stmt)
        record = res.scalar_one_or_none()

        services_str = (
            json.dumps(payload.services)
            if payload.services is not None and not isinstance(payload.services, str)
            else str(payload.services)
            if payload.services
            else None
        )

        cfg_rev = str(payload.config_revision) if payload.config_revision is not None else None
        dep_rev = (
            str(payload.deployment_revision) if payload.deployment_revision is not None else None
        )

        if record:
            record.site_id = payload.site_id
            record.schema_version = payload.schema_version
            record.config_revision = cfg_rev
            record.deployment_revision = dep_rev
            record.camera_id = payload.camera_id
            record.reported_at = payload.reported_at
            record.last_heartbeat_at = now
            record.overall_status = payload.overall_status
            record.status_reason_code = payload.status_reason_code
            record.clock_sync_status = payload.clock_sync_status
            record.clock_offset_ms = payload.clock_offset_ms
            record.services_data = services_str
        else:
            record = EdgeHeartbeatModel(
                edge_id=payload.edge_id,
                site_id=payload.site_id,
                schema_version=payload.schema_version,
                config_revision=cfg_rev,
                deployment_revision=dep_rev,
                camera_id=payload.camera_id,
                reported_at=payload.reported_at,
                last_heartbeat_at=now,
                overall_status=payload.overall_status,
                status_reason_code=payload.status_reason_code,
                clock_sync_status=payload.clock_sync_status,
                clock_offset_ms=payload.clock_offset_ms,
                services_data=services_str,
            )
            db.add(record)

        await db.commit()
        logger.info(
            "Recorded heartbeat for edge '%s' (site: %s, status: %s)",
            payload.edge_id,
            payload.site_id,
            payload.overall_status,
        )

        return EdgeHeartbeatResponse(
            status="ok",
            edge_id=payload.edge_id,
            reported_at=payload.reported_at,
        )

    async def get_latest_heartbeat(
        self,
        edge_id: str,
        db: AsyncSession,
    ) -> EdgeHeartbeatModel | None:
        """Fetch the most recent heartbeat entry for an edge device."""
        stmt = select(EdgeHeartbeatModel).where(EdgeHeartbeatModel.edge_id == edge_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()


edge_service = EdgeService()
