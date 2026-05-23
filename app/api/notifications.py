import asyncio
import logging

from fastapi import WebSocket

log = logging.getLogger("javis.notify")


class ConnectionManager:
    """연결된 클라이언트 묶음.

    토큰 스트리밍과 능동 알림이 같은 소켓에 동시에 쓸 수 있어, 연결마다 락을 두고
    모든 송신을 직렬화한다.
    """

    def __init__(self) -> None:
        self._locks: dict[WebSocket, asyncio.Lock] = {}

    def add(self, ws: WebSocket) -> None:
        self._locks.setdefault(ws, asyncio.Lock())

    def remove(self, ws: WebSocket) -> None:
        self._locks.pop(ws, None)

    @property
    def count(self) -> int:
        return len(self._locks)

    async def send(self, ws: WebSocket, message: dict) -> None:
        lock = self._locks.get(ws)
        if lock is None:
            await ws.send_json(message)
            return
        async with lock:
            await ws.send_json(message)

    async def broadcast(self, message: dict) -> None:
        for ws in list(self._locks):
            try:
                await self.send(ws, message)
            except Exception:
                self.remove(ws)


manager = ConnectionManager()
