"""Root API router."""

from fastapi import APIRouter

from src.app.api.edge_v2 import router as edge_v2_router
from src.app.api.monitoring_v2 import router as monitoring_v2_router
from src.app.api.v1 import api_v1_router

root_api_router = APIRouter()
root_api_router.include_router(api_v1_router, prefix="/api/v1")
root_api_router.include_router(edge_v2_router)
root_api_router.include_router(monitoring_v2_router)
