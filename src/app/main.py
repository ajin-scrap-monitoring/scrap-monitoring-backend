"""Main FastAPI application entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.app.api.router import root_api_router
from src.app.core.config import get_settings
from src.app.core.logging import setup_logging
from src.app.db.session import close_db, init_db

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

    # 3. Initialize database tables
    await init_db()
    logger.info("Application initialized successfully.")

    yield

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
