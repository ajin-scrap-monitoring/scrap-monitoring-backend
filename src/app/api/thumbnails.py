"""Session-protected thumbnail JSON and Nginx internal image handoff."""

from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from src.app.api.errors import MEDIA_ERROR_CODES, error_responses, request_validation_error_response
from src.app.api.v1.media_control import CurrentSession, media_error_response, require_media_admin
from src.app.schemas.thumbnail import ClipThumbnailInfo, DailyThumbnailInfo, ThumbnailRequest
from src.app.services.media_client import MediaServiceError
from src.app.services.thumbnail_service import Thumbnail, paths, thumbnail


async def no_cache(response: Response) -> None:
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


class ThumbnailRoute(APIRoute):
    """Do not cache authentication, request validation or upstream error responses."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await original(request)
            except HTTPException as exc:
                response = await http_exception_handler(request, exc)
            except RequestValidationError as exc:
                response = await request_validation_error_response(request, exc)
            except MediaServiceError as exc:
                response = await media_error_response(request, exc)
            await no_cache(response)
            return response

        return handle


router = APIRouter(
    route_class=ThumbnailRoute,
    tags=["Recording Thumbnails"],
    responses=error_responses(401, 422, *MEDIA_ERROR_CODES, media=True),
)


def validate_request(request: Request, identifier: UUID) -> None:
    if request.query_params:
        raise HTTPException(422, "Thumbnail requests do not accept query parameters")
    if request.path_params.get("identifier") != str(identifier):
        raise HTTPException(422, "Use a canonical lowercase UUID")


@router.get("/api/v1/media/clips/{identifier}/thumbnail-info", response_model=ClipThumbnailInfo)
async def clip_info(identifier: UUID, request: Request, session: CurrentSession) -> Thumbnail:
    validate_request(request, identifier)
    return (await thumbnail(identifier))[1]


@router.get(
    "/api/v2/recordings/daily/{identifier}/thumbnail-info", response_model=DailyThumbnailInfo
)
async def daily_info(identifier: UUID, request: Request, session: CurrentSession) -> Thumbnail:
    validate_request(request, identifier)
    return (await thumbnail(identifier, daily=True))[1]


async def prepare_response(
    identifier: UUID, request: Request, payload: ThumbnailRequest, *, daily: bool
) -> JSONResponse:
    validate_request(request, identifier)
    code, info = await thumbnail(identifier, daily=daily, prepare=payload)
    return JSONResponse(
        status_code=code,
        content=info.model_dump(mode="json"),
        headers={
            "Location": paths(identifier, daily)[1] + "/thumbnail-info",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/api/v1/media/clips/{identifier}/thumbnail",
    response_model=ClipThumbnailInfo,
    dependencies=[Depends(require_media_admin)],
    responses={202: {"model": ClipThumbnailInfo}, **error_responses(403)},
)
async def prepare_clip(
    identifier: UUID, request: Request, payload: ThumbnailRequest
) -> JSONResponse:
    return await prepare_response(identifier, request, payload, daily=False)


@router.post(
    "/api/v2/recordings/daily/{identifier}/thumbnail",
    response_model=DailyThumbnailInfo,
    dependencies=[Depends(require_media_admin)],
    responses={202: {"model": DailyThumbnailInfo}, **error_responses(403)},
)
async def prepare_daily(
    identifier: UUID, request: Request, payload: ThumbnailRequest
) -> JSONResponse:
    return await prepare_response(identifier, request, payload, daily=True)


async def image_response(identifier: UUID, request: Request, *, daily: bool) -> Response:
    validate_request(request, identifier)
    _, info = await thumbnail(identifier, daily=daily)
    if info.status != "ready":
        code = 409 if info.status == "pending" else 503 if info.status == "failed" else 404
        raise MediaServiceError(
            code, "THUMBNAIL_NOT_READY" if code == 409 else "THUMBNAIL_UNAVAILABLE"
        )
    # This header names a fixed Nginx internal location, never an upstream-provided URL.
    prefix = paths(identifier, daily)[0]
    return Response(
        headers={
            "X-Accel-Redirect": f"/_thumbnail/{'v2' if daily else 'v1'}/{prefix}/thumbnail",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
    )


@router.api_route(
    "/media/clips/{identifier}/thumbnail", methods=["GET", "HEAD"], include_in_schema=False
)
async def clip_image(identifier: UUID, request: Request, session: CurrentSession) -> Response:
    return await image_response(identifier, request, daily=False)


@router.api_route(
    "/media/daily-recordings/{identifier}/thumbnail",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def daily_image(identifier: UUID, request: Request, session: CurrentSession) -> Response:
    return await image_response(identifier, request, daily=True)
