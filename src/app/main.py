"""Main FastAPI application entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from src.app.api.errors import request_validation_error_response
from src.app.api.router import root_api_router
from src.app.api.v1.media_control import media_error_response
from src.app.core.config import get_settings
from src.app.core.logging import setup_logging
from src.app.db.session import close_db, get_sessionmaker, init_db
from src.app.services.auth_bootstrap import bootstrap_accounts
from src.app.services.edge_v2_operations import edge_v2_operations_worker
from src.app.services.media_client import MediaServiceError
from src.app.services.media_job_service import media_job_worker

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan manager for startup and shutdown events."""
    # 1. Setup logging
    setup_logging()
    logger.info("Starting Scrap Monitoring Backend...")

    # 2. Fail-Fast credential validation
    settings = get_settings()
    settings.validate_startup_credentials()

    try:
        # 3. Initialize database tables
        await init_db()
        async with get_sessionmaker()() as db:
            await bootstrap_accounts(db)
        logger.info("Application initialized successfully.")

        # Event recording requests and polling recover from the persistent outbox.
        media_job_worker.start()
        edge_v2_operations_worker.start()
        yield
    finally:
        await edge_v2_operations_worker.stop()
        await media_job_worker.stop()
        # 4. Graceful shutdown
        logger.info("Shutting down Scrap Monitoring Backend...")
        await close_db()


def create_app() -> FastAPI:
    """Factory creating configured FastAPI instance."""
    settings = get_settings()

    app = FastAPI(
        title="Ajin Scrap Monitoring Backend API",
        description="Monitoring API, Persistence, Real-time Hub, and Alert Engine",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_exception_handler(MediaServiceError, media_error_response)
    app.add_exception_handler(RequestValidationError, request_validation_error_response)

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount API routers
    app.include_router(root_api_router)

    # Health check alias at root level
    @app.get("/healthz", tags=["Health"])
    async def root_healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
