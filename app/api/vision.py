"""카메라 계층.

카메라는 사용자 PC 에 붙어 있으므로 서버가 프레임을 직접 잡을 수 없다. 비전 데몬이
/ws/vision 으로 붙어 있고, 서버는 필요할 때만 "한 장 줘" 하고 요청한다. 상시 업로드가
아니라 요청-응답이라 평소엔 대역폭도 비용도 들지 않는다.

얼굴 임베딩 계산과 매칭은 데몬(로컬)에서 하고, 서버는 등록된 얼굴을 보관·배포만 한다.
아이 얼굴 같은 민감정보를 클라우드로 보내지 않기 위해서다. 클라우드로 나가는 건
"이게 뭐야" 하고 물었을 때의 프레임 한 장뿐이다.
"""

import asyncio
import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.api.deps import authenticate_ws, require_token
from app.api.hud import hud_manager
from app.config import settings
from app.db.models import KnownFace
from app.db.session import async_session

log = logging.getLogger("javis.vision")
router = APIRouter()

SETUP_HINT = (
    "카메라가 연결되어 있지 않습니다. `python voice_client/jarvis_vision.py` 를 "
    "실행해 주세요."
)

CAPTURE_TIMEOUT = 10.0
# 이보다 오래된 인식 결과는 "지금 누가 있는지"의 답으로 쓰지 않는다.
SIGHTING_TTL = 30.0
MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass
class Sighting:
    names: list[str]
    unknown: int
    at: datetime


# 비전 데몬은 한 대만 붙는 걸 전제로 한다. 카메라가 여럿이면 그때 가서 늘린다.
_client: WebSocket | None = None
_latest: Sighting | None = None
_pending: asyncio.Future | None = None
# 캡처 요청이 겹치면 응답이 어느 요청 것인지 알 수 없다. 한 번에 하나만 보낸다.
_capture_lock = asyncio.Lock()


def connected() -> bool:
    return _client is not None


def latest_sighting() -> Sighting | None:
    """마지막으로 본 사람들. 너무 오래됐으면 없는 셈 친다."""
    if _latest is None:
        return None
    age = (datetime.now(timezone.utc) - _latest.at).total_seconds()
    return _latest if age <= SIGHTING_TTL else None


async def capture() -> tuple[bytes | None, str | None]:
    """데몬에게 프레임 한 장을 요청한다. (jpeg 바이트, 오류메시지)."""
    global _pending

    if _client is None:
        return None, SETUP_HINT

    async with _capture_lock:
        target = _client  # 대기 중에 재연결로 바뀔 수 있어 시점의 소켓을 붙잡는다
        loop = asyncio.get_running_loop()
        _pending = loop.create_future()
        try:
            await target.send_json({"type": "capture"})
            data = await asyncio.wait_for(_pending, timeout=CAPTURE_TIMEOUT)
        except asyncio.TimeoutError:
            return None, "카메라가 제때 응답하지 않았습니다."
        except Exception as exc:
            log.warning("프레임 요청 실패: %s", exc)
            return None, "카메라에 연결하지 못했습니다."
        finally:
            _pending = None

    if isinstance(data, str):  # 데몬이 보낸 오류
        return None, data
    return data, None


def _resolve_frame(payload: dict) -> bytes | str:
    raw = payload.get("image") or ""
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return "카메라가 보낸 이미지를 읽지 못했습니다."
    if not data:
        return "카메라가 빈 이미지를 보냈습니다."
    if len(data) > MAX_FRAME_BYTES:
        return "카메라가 보낸 이미지가 너무 큽니다."
    return data


@router.websocket("/ws/vision")
async def vision_ws(ws: WebSocket) -> None:
    """비전 데몬 전용 채널.

    데몬 → 서버: faces(누가 보이는지), gesture(제스처), frame(캡처 응답)
    서버 → 데몬: capture(한 장 달라)
    """
    global _client, _latest

    await ws.accept()
    if not await authenticate_ws(ws):
        return

    if _client is not None:
        # 데몬을 다시 띄운 경우. 옛 연결은 버리고 새 것을 쓴다.
        try:
            await _client.close(code=1000, reason="replaced")
        except Exception:
            pass
    _client = ws
    log.info("비전 데몬 연결됨")

    try:
        while True:
            payload = await ws.receive_json()
            kind = (payload or {}).get("type")

            if kind == "frame":
                if _pending is not None and not _pending.done():
                    _pending.set_result(_resolve_frame(payload))
            elif kind == "error":
                if _pending is not None and not _pending.done():
                    _pending.set_result(str(payload.get("message") or "카메라 오류"))
            elif kind == "faces":
                names = [str(n) for n in (payload.get("names") or [])]
                _latest = Sighting(
                    names=names,
                    unknown=int(payload.get("unknown") or 0),
                    at=datetime.now(timezone.utc),
                )
                await hud_manager.broadcast(
                    {"state": "vision", "faces": names, "text": ", ".join(names)}
                )
            elif kind == "gesture":
                await hud_manager.broadcast(
                    {"state": "gesture", "text": str(payload.get("name") or "")}
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("비전 채널 오류: %s", exc)
    finally:
        if _client is ws:
            _client = None
            log.info("비전 데몬 연결 끊김")


# --- 등록된 얼굴 ---


async def _notify_faces_changed() -> None:
    """등록·삭제 직후 데몬에게 목록을 다시 받으라고 알린다.

    이게 없으면 얼굴을 등록해 놓고도 데몬을 다시 띄울 때까지 못 알아본다.
    """
    if _client is None:
        return
    try:
        await _client.send_json({"type": "faces_updated"})
    except Exception as exc:
        log.debug("얼굴 갱신 알림 실패(무시): %s", exc)


def _floats(vector) -> list[float]:
    """pgvector 는 numpy 배열을 돌려준다. numpy.float32 는 JSON 으로 못 나간다."""
    return [float(v) for v in vector]


@router.get("/vision/faces", dependencies=[Depends(require_token)])
async def list_faces() -> list[dict]:
    """데몬이 부팅 때 받아 가는 목록. 매칭은 데몬이 로컬에서 한다."""
    async with async_session() as session:
        rows = (await session.execute(select(KnownFace).order_by(KnownFace.name))).scalars().all()
    return [
        {"name": f.name, "relation": f.relation, "embedding": _floats(f.embedding)} for f in rows
    ]


@router.post("/vision/face", dependencies=[Depends(require_token)])
async def enroll_face(payload: dict = Body(...)) -> dict:
    """얼굴 등록. 임베딩은 데몬이 계산해서 보낸다 — 사진은 서버로 오지 않는다."""
    name = str((payload or {}).get("name") or "").strip()
    relation = str((payload or {}).get("relation") or "").strip()
    embedding = (payload or {}).get("embedding") or []

    if not name:
        raise HTTPException(400, "이름이 필요합니다.")
    if len(embedding) != settings.face_embedding_dim:
        raise HTTPException(
            400,
            f"임베딩 차원이 맞지 않습니다 (받은 값 {len(embedding)}, "
            f"설정 {settings.face_embedding_dim}).",
        )

    async with async_session() as session:
        row = (
            await session.execute(select(KnownFace).where(KnownFace.name == name))
        ).scalar_one_or_none()
        if row is None:
            session.add(KnownFace(name=name, relation=relation, embedding=embedding))
            created = True
        else:
            row.relation, row.embedding = relation or row.relation, embedding
            created = False
        await session.commit()

    await _notify_faces_changed()
    return {"ok": True, "name": name, "created": created}


@router.delete("/vision/face/{name}", dependencies=[Depends(require_token)])
async def forget_face(name: str) -> dict:
    async with async_session() as session:
        row = (
            await session.execute(select(KnownFace).where(KnownFace.name == name))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"'{name}' 은(는) 등록돼 있지 않습니다.")
        await session.delete(row)
        await session.commit()

    await _notify_faces_changed()
    return {"ok": True, "name": name}
