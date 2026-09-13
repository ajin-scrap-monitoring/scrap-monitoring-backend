"""Health and readiness check endpoints."""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session

router = APIRouter(tags=["Health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Basic health check verifying the process is running."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(db: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Readiness probe checking database connectivity."""
    try:
        await db.execute(text("SELECT 1"))
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready", "database": "connected"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "database": str(exc)},
        )
