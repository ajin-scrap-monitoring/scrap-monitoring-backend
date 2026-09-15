"""Session and authentication endpoints conforming to proposal/openapi.yaml."""

from fastapi import APIRouter, Cookie, Header, HTTPException, Response, status

from src.app.core.config import get_settings
from src.app.schemas.session import LoginRequest, Session
from src.app.services.session_service import SESSION_COOKIE_NAME, session_service

router = APIRouter(prefix="/session", tags=["Session"])


@router.get(
    "",
    response_model=Session,
    summary="Get the current browser session",
)
async def get_session(
    scrap_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Session:
    """Get the current authenticated browser session."""
    if not scrap_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session cookie missing",
        )

    session = session_service.get_session(scrap_session)
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
) -> Session:
    """Authenticate user credentials and create a session with HttpOnly cookie."""
    user = session_service.authenticate(username=payload.username, password=payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    session_id, session_obj = session_service.create_session(
        user=user, persistent=payload.persistent
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
    scrap_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    """Delete the active browser session and clear session cookie."""
    if not scrap_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: session cookie missing",
        )

    session = session_service.get_session(scrap_session)
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

    session_service.delete_session(scrap_session)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
