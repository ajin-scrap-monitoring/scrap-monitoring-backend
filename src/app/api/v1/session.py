"""Session and authentication endpoints conforming to proposal/openapi.yaml."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.schemas.session import LoginRequest, Session
from src.app.services.session_service import SESSION_COOKIE_NAME, session_service


class SessionRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                # Validation failures must not echo a submitted password or cookie.
                response = JSONResponse(
                    status_code=422, content={"detail": "Invalid session request"}
                )
            except SQLAlchemyError:
                response = JSONResponse(
                    status_code=503,
                    content={"detail": "Authentication storage unavailable"},
                    headers={"Retry-After": "1"},
                )
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe_handler


router = APIRouter(prefix="/session", tags=["Session"], route_class=SessionRoute)
Database = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "",
    response_model=Session,
    summary="Get the current browser session",
)
async def get_session(
    db: Database,
    scrap_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Session:
    """Get the current authenticated browser session."""
    if not scrap_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session cookie missing",
        )

    try:
        session = await session_service.get_session(db, scrap_session)
    except SQLAlchemyError:
        raise HTTPException(503, "Authentication storage unavailable") from None
    finally:
        # Auth dependencies must not hold a read transaction during media I/O or SSE waits.
        await db.rollback()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session expired or invalid",
        )

    return session


@router.post(
    "",
    response_model=Session,
    summary="Create a browser session",
)
async def create_session(
    payload: LoginRequest,
    response: Response,
    db: Database,
) -> Session:
    """Authenticate user credentials and create a session with HttpOnly cookie."""
    user = await session_service.authenticate(
        db, username=payload.username, password=payload.password.get_secret_value()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    session_id, session_obj = await session_service.create_session(
        db, user=user, persistent=payload.persistent
    )

    settings = get_settings()
    is_secure = settings.APP_ENV == "production"
    # Max-age: 30 days for persistent, 24 hours for normal
    max_age = 30 * 24 * 3600 if payload.persistent else 24 * 3600

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )
    return session_obj


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete the current browser session",
)
async def delete_session(
    response: Response,
    db: Database,
    scrap_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """Delete the active browser session and clear session cookie."""
    if not scrap_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session cookie missing",
        )

    session = await session_service.get_session(db, scrap_session)
    if not session:
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session expired or invalid",
        )

    # If CSRF token is provided, verify it matches
    if x_csrf_token and x_csrf_token != session.csrfToken:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: CSRF token mismatch",
        )

    await session_service.delete_session(db, scrap_session)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
