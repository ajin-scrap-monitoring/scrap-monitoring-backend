"""V1 API router aggregation."""

from fastapi import APIRouter

from src.app.api.v1.admin import (
    notifications_admin_router,
    recipients_router,
    settings_router,
)
from src.app.api.v1.collections import router as collections_router
from src.app.api.v1.config import router as config_router
from src.app.api.v1.edge import router as edge_router
from src.app.api.v1.health import router as health_router
from src.app.api.v1.history import events_router, history_router
from src.app.api.v1.media import router as media_router
from src.app.api.v1.metrics import router as metrics_router
from src.app.api.v1.monitoring import router as monitoring_router
from src.app.api.v1.notifications import router as notifications_router
from src.app.api.v1.recordings import router as recordings_router
from src.app.api.v1.session import router as session_router
from src.app.api.v1.stream import router as stream_router

api_v1_router = APIRouter()
api_v1_router.include_router(session_router)

api_v1_router.include_router(health_router)
api_v1_router.include_router(metrics_router)
api_v1_router.include_router(edge_router)
api_v1_router.include_router(config_router)

api_v1_router.include_router(stream_router)
api_v1_router.include_router(collections_router)
api_v1_router.include_router(media_router)
api_v1_router.include_router(monitoring_router)
api_v1_router.include_router(history_router)
api_v1_router.include_router(events_router)
api_v1_router.include_router(recordings_router)
api_v1_router.include_router(settings_router)
api_v1_router.include_router(recipients_router)
api_v1_router.include_router(notifications_router)
api_v1_router.include_router(notifications_admin_router)
