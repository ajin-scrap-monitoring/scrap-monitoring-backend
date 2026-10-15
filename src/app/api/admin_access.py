"""Browser session permissions for administration and configuration APIs."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Response

from src.app.api.v1.session import get_session
from src.app.schemas.session import Session

CurrentSession = Annotated[Session, Depends(get_session)]


async def private_response(response: Response) -> None:
    """Settings and recipient information must not be cached by shared clients."""
    response.headers["Cache-Control"] = "no-store"


async def require_administrator(session: CurrentSession) -> Session:
    """Read current DB permissions rather than trusting the browser's role display."""
    if session.user.role != "administrator":
        raise HTTPException(403, "Administrator access required")
    return session


async def require_admin_change(
    session: Annotated[Session, Depends(require_administrator)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Session:
    """Changing settings requires the nonce issued for this same browser session."""
    if csrf_token is None or csrf_token != session.csrfToken:
        raise HTTPException(403, "Invalid or missing CSRF token")
    return session
