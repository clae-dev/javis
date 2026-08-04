"""필립스 휴 조명.

브리지의 로컬 CLIP v2 API 를 직접 부른다. 클라우드를 거치지 않아 인터넷이 끊겨도
집 안에서는 그대로 동작한다.

이름 매칭은 느슨하게 본다 — 음성으로 "거실 불 꺼줘" 하면 STT 가 "거실"을 정확히
받아적는다는 보장이 없다. 조명 하나뿐 아니라 방(그룹)도 같은 이름 공간에서 찾는다.
"""

import asyncio
import logging
from typing import Any

import httpx
from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger("javis.hue")

SETUP_HINT = (
    "휴 연결이 안 되어 있습니다. 브리지 가운데 버튼을 누른 뒤 "
    "`python scripts/hue_auth.py` 를 한 번 실행하고, 나온 값을 .env 의 "
    "HUE_APP_KEY 에 넣어 주세요."
)

DISCOVERY_URL = "https://discovery.meethue.com"
_TIMEOUT = 8.0

# 브리지 인증서는 브리지 ID 로 자체 서명돼 있다. 필립스 루트 CA 를 번들해 CN 까지
# 맞춰 검증할 수도 있지만, 대상이 같은 랜 안의 고정 장비라 실익이 적다. 대신 브리지
# 주소를 설정이나 공식 디스커버리로만 얻어 임의 호스트로 새지 않게 막는다.
_VERIFY = False

_bridge_ip: str | None = None
_lock = asyncio.Lock()


# --- 브리지 찾기 ---


async def _discover() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            res = await client.get(DISCOVERY_URL)
            res.raise_for_status()
            data = res.json()
    except Exception as exc:
        log.warning("휴 브리지 자동 탐색 실패: %s", exc)
        return None
    for entry in data if isinstance(data, list) else []:
        if ip := (entry or {}).get("internalipaddress"):
            return str(ip)
    return None


async def _bridge() -> str | None:
    """브리지 IP. 설정값이 있으면 그대로, 없으면 한 번 탐색해 캐시한다."""
    global _bridge_ip
    if settings.hue_bridge_ip:
        return settings.hue_bridge_ip
    async with _lock:
        if _bridge_ip is None:
            _bridge_ip = await _discover()
    return _bridge_ip


# --- HTTP ---


async def _request(method: str, path: str, payload: dict | None = None) -> tuple[Any, str | None]:
    """(데이터, 오류메시지). 오류메시지가 있으면 데이터는 무시한다."""
    if not settings.hue_app_key:
        return None, SETUP_HINT

    ip = await _bridge()
    if not ip:
        return None, "휴 브리지를 찾지 못했습니다. .env 의 HUE_BRIDGE_IP 에 주소를 직접 넣어 주세요."

    url = f"https://{ip}/clip/v2/resource{path}"
    headers = {"hue-application-key": settings.hue_app_key}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=_VERIFY) as client:
            res = await client.request(method, url, headers=headers, json=payload)
    except Exception as exc:
        return None, f"휴 브리지에 연결하지 못했습니다: {exc}"

    if res.status_code == 403:
        return None, SETUP_HINT
    if res.status_code >= 400:
        return None, f"휴 브리지가 요청을 거부했습니다 ({res.status_code})."

    try:
        body = res.json()
    except Exception:
        return None, "휴 브리지 응답을 읽지 못했습니다."

    if errors := body.get("errors"):
        detail = "; ".join(str(e.get("description", e)) for e in errors)
        return None, f"휴 오류: {detail}"
    return body.get("data", []), None


# --- 이름 매칭 ---


def _norm(text: str) -> str:
    return text.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _match(name: str, targets: list[dict]) -> list[dict]:
    """이름으로 대상을 고른다. 정확히 → 부분일치 순. '전체'는 전부."""
    key = _norm(name)
    if key in {"전체", "전부", "모두", "다", "all", "everything"}:
        return targets
    if not key:
        return []
    exact = [t for t in targets if _norm(t["name"]) == key]
    if exact:
        return exact
    return [t for t in targets if key in _norm(t["name"]) or _norm(t["name"]) in key]


# --- 색 ---

# 브리지는 CIE xy 로만 색을 받는다. 자주 부르는 색만 미리 표로 둔다.
_COLORS: dict[str, tuple[float, float]] = {
    "빨강": (0.675, 0.322),
    "주황": (0.556, 0.408),
    "노랑": (0.444, 0.517),
    "초록": (0.215, 0.711),
    "청록": (0.170, 0.340),
    "파랑": (0.167, 0.040),
    "남색": (0.152, 0.061),
    "보라": (0.270, 0.110),
    "분홍": (0.400, 0.200),
    "하양": (0.323, 0.329),
}
_COLOR_ALIASES = {
    "빨간색": "빨강", "적색": "빨강", "레드": "빨강", "red": "빨강",
    "주황색": "주황", "오렌지": "주황", "orange": "주황",
    "노란색": "노랑", "옐로": "노랑", "yellow": "노랑",
    "초록색": "초록", "녹색": "초록", "그린": "초록", "green": "초록",
    "민트": "청록", "cyan": "청록",
    "파란색": "파랑", "블루": "파랑", "blue": "파랑",
    "네이비": "남색",
    "보라색": "보라", "퍼플": "보라", "purple": "보라",
    "핑크": "분홍", "pink": "분홍",
    "흰색": "하양", "하얀색": "하양", "화이트": "하양", "white": "하양",
}


def _xy(color: str) -> tuple[float, float] | None:
    key = _norm(color)
    key = _COLOR_ALIASES.get(key, key)
    return _COLORS.get(key)


# --- 조회 ---


async def _targets() -> tuple[list[dict], str | None]:
    """조명과 방을 한 이름 공간으로 합친다.

    방은 grouped_light 라는 별도 리소스로 제어하므로, 방 이름 → 그 방의 grouped_light
    id 로 미리 이어 둔다. 그래야 "거실 꺼줘" 한 번에 그 방 전체가 꺼진다.
    """
    lights, err = await _request("GET", "/light")
    if err:
        return [], err

    targets = [
        {
            "kind": "light",
            "id": light["id"],
            "name": (light.get("metadata") or {}).get("name", ""),
            "on": bool((light.get("on") or {}).get("on")),
            "brightness": round((light.get("dimming") or {}).get("brightness", 0)),
            "color": "color" in light,
        }
        for light in lights or []
    ]

    rooms, err = await _request("GET", "/room")
    if err:
        # 방 조회가 실패해도 개별 조명은 쓸 수 있게 둔다.
        log.debug("휴 방 조회 실패(무시): %s", err)
        return targets, None

    for room in rooms or []:
        group = next(
            (s["rid"] for s in room.get("services", []) if s.get("rtype") == "grouped_light"),
            None,
        )
        if not group:
            continue
        members = {c["rid"] for c in room.get("children", []) if c.get("rtype") == "device"}
        targets.append(
            {
                "kind": "room",
                "id": group,
                "name": (room.get("metadata") or {}).get("name", ""),
                "on": None,
                "brightness": None,
                "color": True,
                "members": len(members),
            }
        )
    return targets, None


@tool
async def get_hue_lights() -> list[dict] | str:
    """집 안 조명과 방 목록을 본다. 각 조명의 켜짐 여부와 밝기를 함께 준다.

    조명을 조작하기 전에 어떤 이름이 있는지 확인할 때 쓴다.
    """
    targets, err = await _targets()
    if err:
        return err
    if not targets:
        return "등록된 조명이 없습니다."
    return [
        {k: v for k, v in t.items() if k != "id" and v is not None}
        for t in targets
    ]


# --- 제어 ---


@tool
async def set_hue_light(
    name: str,
    on: bool | None = None,
    brightness: int | None = None,
    color: str = "",
) -> str:
    """조명이나 방의 불을 켜고 끄고, 밝기·색을 바꾼다. 실행 전 사용자 확인을 거친다.

    Args:
        name: 조명이나 방 이름. "전체"를 넣으면 전부. 정확하지 않아도 비슷하면 찾는다.
        on: True 면 켜기, False 면 끄기. 밝기나 색만 바꿀 거면 비운다.
        brightness: 밝기 1~100. 0 을 주면 끈다.
        color: 색 이름 (빨강/주황/노랑/초록/청록/파랑/남색/보라/분홍/하양).
    """
    targets, err = await _targets()
    if err:
        return err

    hits = _match(name, targets)
    if not hits:
        names = ", ".join(sorted({t["name"] for t in targets if t["name"]})) or "(없음)"
        return f"'{name}' 을(를) 찾지 못했습니다. 있는 이름: {names}"

    body: dict[str, Any] = {}
    if brightness is not None:
        brightness = max(0, min(int(brightness), 100))
        if brightness == 0:
            on = False
        else:
            body["dimming"] = {"brightness": float(brightness)}
            if on is None:
                on = True  # 밝기를 올리라는 건 켜라는 뜻이다
    if color:
        xy = _xy(color)
        if xy is None:
            return f"'{color}' 색은 아직 모릅니다. 쓸 수 있는 색: {', '.join(_COLORS)}"
        body["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
        if on is None:
            on = True
    if on is not None:
        body["on"] = {"on": bool(on)}

    if not body:
        return "무엇을 바꿀지 알려주세요 (켜기/끄기, 밝기, 색)."

    # 조명 여러 개를 동시에 건드려도 브리지가 순서를 요구하지 않는다.
    async def apply(target: dict) -> tuple[str, str | None]:
        path = f"/{'grouped_light' if target['kind'] == 'room' else 'light'}/{target['id']}"
        _, error = await _request("PUT", path, body)
        return target["name"], error

    results = await asyncio.gather(*(apply(t) for t in hits))

    ok = [n for n, e in results if e is None]
    failed = [(n, e) for n, e in results if e is not None]

    parts = []
    if ok:
        what = []
        if "on" in body:
            what.append("켬" if body["on"]["on"] else "끔")
        if "dimming" in body:
            what.append(f"밝기 {int(body['dimming']['brightness'])}")
        if "color" in body:
            what.append(f"{color} 색")
        parts.append(f"{', '.join(ok)} — {' / '.join(what)}")
    if failed:
        parts.append("실패: " + "; ".join(f"{n}({e})" for n, e in failed))
    return " | ".join(parts)
