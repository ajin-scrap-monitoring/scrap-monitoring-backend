"""V1 API router aggregation."""

from fastapi import APIRouter

from src.app.api.v1.health import router as health_router
from src.app.api.v1.metrics import router as metrics_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router)
api_v1_router.include_router(metrics_router)
