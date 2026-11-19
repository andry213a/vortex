# WebSocket exclusivo del host para la consola de logs.

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..security import is_loopback

router = APIRouter()


# Autorización del WebSocket de logs reservado al host.
def _websocket_role(websocket: WebSocket, runtime, token: str | None) -> str | None:
    if token:
        carga = runtime.auth.verify(token)
        if carga:
            return str(carga.get("role") or "") or None
    configuracion = runtime.config
    anfitrion = websocket.client.host if websocket.client else None
    if configuracion.auth_mode == "none" and is_loopback(anfitrion):
        return "host"
    return None


@router.websocket("/ws/logs")
async def logs_socket(websocket: WebSocket, token: str | None = None) -> None:
    entorno = websocket.app.state.runtime
    if _websocket_role(websocket, entorno, token) != "host":
        await websocket.close(code=1008)
        return
    await websocket.accept()
    bucle = asyncio.get_running_loop()
    cola: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def listener(record: dict) -> None:
        def enqueue() -> None:
            try:
                cola.put_nowait(record)
            except asyncio.QueueFull:
                pass

        bucle.call_soon_threadsafe(enqueue)

    desuscribir = entorno.audit.add_listener(listener)
    try:
        for registro in entorno.audit.tail(50):
            await websocket.send_json(registro)
        while True:
            registro = await cola.get()
            await websocket.send_json(registro)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        desuscribir()
