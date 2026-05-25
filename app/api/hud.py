"""HUD(영화 같은 AI 화면) 이벤트 채널.

음성 데몬이 상태를 POST /hud/event 로 보내면, 연결된 HUD 화면(브라우저)들에
WebSocket 으로 실시간 브로드캐스트한다.
"""

from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect

from app.api.notifications import ConnectionManager

router = APIRouter()
hud_manager = ConnectionManager()


@router.websocket("/ws/hud")
async def hud_ws(ws: WebSocket) -> None:
    await ws.accept()
    hud_manager.add(ws)
    try:
        while True:
            await ws.receive_text()  # 클라이언트 핑(keepalive). 내용은 무시.
    except WebSocketDisconnect:
        pass
    finally:
        hud_manager.remove(ws)


@router.post("/hud/event")
async def hud_event(payload: dict = Body(...)) -> dict:
    """음성 데몬이 호출. {state, text} 를 모든 HUD 화면에 흘린다."""
    await hud_manager.broadcast(payload or {})
    return {"ok": True}
