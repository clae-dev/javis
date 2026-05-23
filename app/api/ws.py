import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.types import Command

from app.agent.runtime import runtime
from app.api.notifications import manager
from app.config import settings

log = logging.getLogger("javis.ws")
router = APIRouter()


async def _stream(ws: WebSocket, payload, config) -> bool:
    """그래프를 한 번 흘려보낸다.

    응답 토큰과 도구 상태를 클라이언트로 push 하고, 확인이 필요한 지점(interrupt)을
    만나면 confirm_request 를 보낸 뒤 True 를 돌려준다(=재개 대기).
    """
    async for mode, chunk in runtime.graph.astream(
        payload, config=config, stream_mode=["messages", "updates"]
    ):
        if mode == "messages":
            msg, meta = chunk
            # 최종 응답(agent 노드)의 토큰만 흘려보낸다. 분류·반추 토큰은 거른다.
            if (
                meta.get("langgraph_node") == "agent"
                and isinstance(msg, AIMessageChunk)
                and msg.content
            ):
                await manager.send(ws, {"type": "token", "content": msg.content})
            continue

        # mode == "updates"
        if "__interrupt__" in chunk:
            interrupt = chunk["__interrupt__"][0]
            await manager.send(ws, {"type": "confirm_request", "data": interrupt.value})
            return True

        for node, update in chunk.items():
            if not isinstance(update, dict):
                continue
            for m in update.get("messages", []):
                for call in getattr(m, "tool_calls", None) or []:
                    await manager.send(ws, {"type": "tool", "status": "running", "name": call["name"]})
            if node == "tools":
                await manager.send(ws, {"type": "tool", "status": "done"})
    return False


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    manager.add(ws)
    thread_id = ws.query_params.get("thread_id", "default")
    config = {"configurable": {"thread_id": thread_id}}
    profile = {"name": settings.owner_name}

    try:
        while True:
            data = await ws.receive_json()
            content = (data or {}).get("content", "").strip()
            if not content:
                continue

            payload = {
                "messages": [HumanMessage(content=content)],
                "user_profile": profile,
            }

            # interrupt 가 걸리면 confirm 응답을 받아 재개. 여러 번 걸릴 수도 있어 루프.
            while await _stream(ws, payload, config):
                reply = await ws.receive_json()
                payload = Command(resume=bool((reply or {}).get("approved")))

            await manager.send(ws, {"type": "done"})

    except WebSocketDisconnect:
        return
    except Exception as exc:
        log.exception("WS 처리 오류")
        try:
            await manager.send(ws, {"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        manager.remove(ws)
