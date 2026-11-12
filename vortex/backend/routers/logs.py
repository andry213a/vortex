from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..security import is_loopback

router = APIRouter()


def _websocket_role(websocket: WebSocket, runtime, token: str | None) -> str | None:
    if token:
        payload = runtime.auth.verify(token)
        if payload:
            return str(payload.get("role") or "") or None
    config = runtime.config
    host = websocket.client.host if websocket.client else None
    if config.auth_mode == "none" and is_loopback(host):
        return "host"
    return None


@router.websocket("/ws/logs")
async def logs_socket(websocket: WebSocket, token: str | None = None) -> None:
    runtime = websocket.app.state.runtime
    if _websocket_role(websocket, runtime, token) != "host":
        await websocket.close(code=1008)
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def listener(record: dict) -> None:
        def enqueue() -> None:
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                pass

        loop.call_soon_threadsafe(enqueue)

    remove = runtime.audit.add_listener(listener)
    try:
        for record in runtime.audit.tail(50):
            await websocket.send_json(record)
        while True:
            record = await queue.get()
            await websocket.send_json(record)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        remove()
