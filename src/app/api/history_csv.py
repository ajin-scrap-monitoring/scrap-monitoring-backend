"""Authenticated CSV downloads; JSON endpoints and their defaults remain unchanged."""

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from src.app.api.errors import error_responses
from src.app.api.monitoring_v2 import CurrentSession, Database, HistoryIntervalQuery, Source
from src.app.schemas.history import CollectionScope
from src.app.services.history_csv import events_csv, load_csv


class CsvRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": "Invalid CSV request"})
            except SQLAlchemyError:
                response = JSONResponse(
                    status_code=503, content={"detail": "CSV_STORAGE_UNAVAILABLE"}
                )
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return safe_handler


router = APIRouter(
    tags=["History CSV"],
    route_class=CsvRoute,
    responses=error_responses(401, 404, 422, 503),
)
From = Annotated[datetime | None, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]
CSV_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {"schema": {"type": "string"}}}}
}


def download(body: bytes, count: int, name: str) -> Response:
    filename = f"{name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-CSV-Row-Count": str(count),
        },
    )


@router.get(
    "/api/v2/monitoring/load-history.csv",
    response_class=Response,
    responses=CSV_RESPONSE,
    summary="Download representative load history as CSV",
)
async def download_load_history(
    session: CurrentSession,
    source: Source,
    db: Database,
    from_time: From = None,
    to_time: To = None,
    bucket_seconds: Annotated[HistoryIntervalQuery | None, Query()] = None,
) -> Response:
    body, count = await load_csv(db, source, from_time, to_time, bucket_seconds)
    return download(body, count, "load-history")


@router.get(
    "/api/v1/events.csv",
    response_class=Response,
    responses=CSV_RESPONSE,
    summary="Download collection, alert and error history as CSV",
)
async def download_event_history(
    session: CurrentSession,
    source: Source,
    db: Database,
    from_time: From = None,
    to_time: To = None,
    category: Literal["all", "collection", "alert", "error"] = "all",
    collection_scope: CollectionScope = "current",
) -> Response:
    body, count = await events_csv(db, source, from_time, to_time, category, collection_scope)
    return download(body, count, "events")
