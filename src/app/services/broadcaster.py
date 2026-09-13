"""Real-time Broadcaster for WebSocket and Server-Sent Events (SSE)."""

import asyncio
import logging

from fastapi import WebSocket

from src.app.schemas.state import RealtimeStatePayload

logger = logging.getLogger(__name__)


class RealtimeBroadcaster:
    """Manages connected dashboard clients and broadcasts real-time state."""

    def __init__(self) -> None:
        self._active_websockets: set[WebSocket] = set()
        self._sse_queues: set[asyncio.Queue[str]] = set()
        self._latest_state: RealtimeStatePayload | None = None
        self._lock = asyncio.Lock()

    @property
    def latest_state(self) -> RealtimeStatePayload | None:
        """Get cached latest scrap state."""
        return self._latest_state

    async def connect_websocket(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket client, sending latest state if available."""
        await websocket.accept()
        async with self._lock:
            self._active_websockets.add(websocket)
        logger.info("WebSocket client connected. Total active: %d", len(self._active_websockets))

        # Send latest state immediately upon connection
        if self._latest_state:
            try:
                await websocket.send_json(self._latest_state.model_dump(mode="json"))
            except Exception as exc:
                logger.warning("Failed to send initial state to WebSocket: %s", exc)

    async def disconnect_websocket(self, websocket: WebSocket) -> None:
        """Unregister a disconnected WebSocket client."""
        async with self._lock:
            self._active_websockets.discard(websocket)
        logger.info("WebSocket client disconnected. Total active: %d", len(self._active_websockets))

    async def register_sse_queue(self) -> asyncio.Queue[str]:
        """Create and register a queue for an SSE subscriber."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._sse_queues.add(queue)
        if self._latest_state:
            await queue.put(self._latest_state.model_dump_json())
        return queue

    async def unregister_sse_queue(self, queue: asyncio.Queue[str]) -> None:
        """Unregister an SSE subscriber queue."""
        async with self._lock:
            self._sse_queues.discard(queue)

    async def broadcast(self, state: RealtimeStatePayload) -> None:
        """Broadcast updated state to all connected WebSocket and SSE clients."""
        async with self._lock:
            self._latest_state = state
            ws_targets = list(self._active_websockets)
            sse_targets = list(self._sse_queues)

        data_json = state.model_dump_json()
        data_dict = state.model_dump(mode="json")

        # Broadcast to WebSockets
        disconnected_ws: list[WebSocket] = []
        for ws in ws_targets:
            try:
                await ws.send_json(data_dict)
            except Exception:
                disconnected_ws.append(ws)

        if disconnected_ws:
            async with self._lock:
                for ws in disconnected_ws:
                    self._active_websockets.discard(ws)

        # Broadcast to SSE queues
        for queue in sse_targets:
            try:
                queue.put_nowait(data_json)
            except asyncio.QueueFull:
                logger.warning("SSE queue full, skipping frame for slow client")


# Global Broadcaster Singleton
broadcaster = RealtimeBroadcaster()
