import asyncio
import json
import logging

from fastapi import WebSocket

log = logging.getLogger("javis.notify")


def _encode(message: dict) -> str:
    # starlette 의 send_json 과 같은 표현. 직접 만들어 두면 연결이 여럿일 때 한 번만 만든다.
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


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

    async def _send_text(self, ws: WebSocket, text: str) -> None:
        lock = self._locks.get(ws)
        if lock is None:
            await ws.send_text(text)
            return
        async with lock:
            await ws.send_text(text)

    async def send(self, ws: WebSocket, message: dict) -> None:
        await self._send_text(ws, _encode(message))

    async def broadcast(self, message: dict) -> None:
        """모든 연결에 같은 내용을 흘린다.

        직렬화는 한 번만 한다 — 브라우저 피드 한 프레임은 base64 로 수십 KB 라,
        연결마다 dict 를 다시 JSON 으로 만들면 그 비용이 연결 수만큼 곱해진다.

        전송은 동시에 건다. 순서대로 기다리면 느린 연결 하나가 나머지 화면까지
        붙잡아, 실제로는 붙어 있는 사람이 많을수록 다 같이 느려진다.
        """
        targets = list(self._locks)
        if not targets:
            return
        text = _encode(message)
        results = await asyncio.gather(
            *(self._send_text(ws, text) for ws in targets), return_exceptions=True
        )
        for ws, result in zip(targets, results):
            if isinstance(result, BaseException):
                self.remove(ws)


manager = ConnectionManager()
