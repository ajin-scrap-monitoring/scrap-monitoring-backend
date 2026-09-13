"""Real-time streaming endpoints via WebSocket and Server-Sent Events (SSE)."""

import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from src.app.services.broadcaster import broadcaster

router = APIRouter(prefix="/stream", tags=["Stream"])


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time scrap loading state push."""
    await broadcaster.connect_websocket(websocket)
    try:
        while True:
            # Keepalive listener (ignores text payloads or client pings)
            await websocket.receive_text()
    except WebSocketDisconnect:
        await broadcaster.disconnect_websocket(websocket)
    except Exception:
        await broadcaster.disconnect_websocket(websocket)


@router.get("/sse", summary="Server-Sent Events real-time stream")
async def sse_endpoint() -> StreamingResponse:
    """SSE endpoint streaming scrap updates to web dashboards."""

    async def event_generator() -> AsyncGenerator[str]:
        queue = await broadcaster.register_sse_queue()
        try:
            while True:
                data = await queue.get()
                yield f"data: {data}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            await broadcaster.unregister_sse_queue(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
