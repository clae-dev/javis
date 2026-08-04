"""접속 인증.

JAVIS_TOKEN 이 비어 있으면 인증을 걸지 않는다 — 로컬에서 혼자 띄워 쓰는 경우다.
값이 있으면 REST 는 헤더로, WebSocket 은 첫 프레임으로 같은 토큰을 요구한다.

WebSocket 토큰을 쿼리파라미터로 받지 않는 이유는, URL 이 접속 로그·프록시
기록·브라우저 히스토리에 그대로 남기 때문이다.
"""

import asyncio
import hmac
import json
import logging

from fastapi import Header, HTTPException, WebSocket

from app.config import settings

log = logging.getLogger("javis.auth")

AUTH_TIMEOUT = 5.0  # 첫 프레임을 기다리는 시간(초)


def _matches(candidate: str | None) -> bool:
    expected = settings.javis_token
    if not expected:
        return True
    return bool(candidate) and hmac.compare_digest(candidate, expected)


async def require_token(
    x_javis_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """REST 라우터용 의존성. X-Javis-Token 헤더나 Bearer 토큰을 본다."""
    candidate = x_javis_token
    if not candidate and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if not _matches(candidate):
        raise HTTPException(401, "접속 토큰이 필요합니다.")


async def authenticate_ws(ws: WebSocket) -> bool:
    """accept() 직후 호출. 통과하면 True, 아니면 소켓을 닫고 False.

    인증이 꺼져 있으면 첫 프레임을 소비하지 않고 그대로 통과시킨다.
    """
    if not settings.javis_token:
        return True

    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT)
        token = (json.loads(raw) or {}).get("token")
    except asyncio.TimeoutError:
        log.warning("WS 인증 시간 초과")
        token = None
    except Exception:
        # 잘못된 JSON, 조기 종료 등. 사유를 구분해 봐야 대응이 같다.
        log.warning("WS 인증 프레임을 읽지 못함")
        token = None

    if _matches(token):
        return True

    try:
        await ws.close(code=1008, reason="unauthorized")
    except Exception:
        pass
    return False
