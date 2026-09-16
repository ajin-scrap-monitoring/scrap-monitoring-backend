"""Edge Platform integration and heartbeat endpoints."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import verify_edge_api_key
from src.app.db.session import get_db_session
from src.app.schemas.edge import EdgeHeartbeatRequest, EdgeHeartbeatResponse
from src.app.services.edge_service import edge_service

router = APIRouter(prefix="/edge", tags=["Edge Platform"])


@router.post(
    "/heartbeat",
    response_model=EdgeHeartbeatResponse,
    status_code=status.HTTP_200_OK,
    summary="Record edge device heartbeat",
)
async def record_heartbeat(
    payload: EdgeHeartbeatRequest,
    db: AsyncSession = Depends(get_db_session),
    _edge_auth: str = Depends(verify_edge_api_key),
) -> EdgeHeartbeatResponse:
    """Receive periodic health and status reports from Edge Platform orchestrator."""
    return await edge_service.record_heartbeat(payload, db)
