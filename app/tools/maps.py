"""장소 검색과 길찾기 (카카오).

운전 중에 쓰라고 만들었다. 지도를 띄워 눈으로 고르는 게 아니라, 몇 분 걸리는지
말로 듣고 판단하는 용도다. 그래서 좌표나 경로 폴리라인은 돌려주지 않고 소요시간·
거리·통행료만 추린다.

REST API 키 하나로 장소 검색(Local)과 길찾기(Mobility)를 둘 다 쓴다.
"""

import asyncio
import logging

import httpx
from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger("javis.maps")

SETUP_HINT = (
    "카카오 지도 연결이 안 되어 있습니다. developers.kakao.com 에서 앱을 만들고 "
    "REST API 키를 .env 의 KAKAO_REST_API_KEY 에 넣어 주세요. "
    "(카카오모빌리티 길찾기는 별도 신청이 필요할 수 있습니다.)"
)

SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
DIRECTIONS_URL = "https://apis-navi.kakaomobility.com/v1/directions"
_TIMEOUT = 10.0

# 카카오 길찾기 우선순위. 사람이 말하는 표현도 같이 받는다.
_PRIORITY = {
    "recommend": "RECOMMEND",
    "추천": "RECOMMEND",
    "time": "TIME",
    "빠른": "TIME",
    "최단시간": "TIME",
    "distance": "DISTANCE",
    "짧은": "DISTANCE",
    "최단거리": "DISTANCE",
}


def _headers() -> dict:
    return {"Authorization": f"KakaoAK {settings.kakao_rest_api_key}"}


def _minutes(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}분"
    return f"{minutes // 60}시간 {minutes % 60}분" if minutes % 60 else f"{minutes // 60}시간"


def _km(meters: int) -> str:
    return f"{meters / 1000:.1f}km" if meters >= 1000 else f"{meters}m"


async def _get(url: str, params: dict) -> tuple[dict | None, str | None]:
    if not settings.kakao_rest_api_key:
        return None, SETUP_HINT
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            res = await client.get(url, params=params, headers=_headers())
    except Exception as exc:
        return None, f"지도 서비스에 연결하지 못했습니다: {exc}"

    if res.status_code == 401:
        return None, SETUP_HINT
    if res.status_code == 403:
        return None, "카카오 길찾기 사용 권한이 없습니다. 개발자 콘솔에서 신청해 주세요."
    if res.status_code >= 400:
        return None, f"지도 요청이 거부됐습니다 ({res.status_code})."

    try:
        return res.json(), None
    except Exception:
        return None, "지도 응답을 읽지 못했습니다."


async def _geocode(query: str) -> tuple[dict | None, str | None]:
    """장소 이름을 좌표로 바꾼다. 첫 번째 결과를 쓴다."""
    query = query.strip()
    if not query:
        return None, "장소를 알려주세요."

    data, err = await _get(SEARCH_URL, {"query": query, "size": 1})
    if err:
        return None, err

    documents = data.get("documents") or []
    if not documents:
        return None, f"'{query}' 을(를) 지도에서 찾지 못했습니다."

    place = documents[0]
    return {
        "name": place.get("place_name", query),
        "address": place.get("road_address_name") or place.get("address_name", ""),
        "x": place["x"],
        "y": place["y"],
    }, None


# --- 도구 ---


@tool
async def search_place(query: str, near: str = "") -> list[dict] | str:
    """장소를 찾는다. 이름·주소·전화번호와, 기준점을 주면 거리까지 돌려준다.

    "근처 맥도날드", "제주공항 주차장" 처럼 어디로 갈지 고를 때 쓴다.

    Args:
        query: 찾을 장소 (예: "맥도날드", "스타벅스 강남").
        near: 이 지점 기준으로 가까운 순으로 찾는다. 비우면 설정된 집 주소를 쓴다.
    """
    params: dict = {"query": query.strip(), "size": 5}
    if not params["query"]:
        return "무엇을 찾을지 알려주세요."

    origin = near.strip() or settings.home_address
    if origin:
        anchor, err = await _geocode(origin)
        if err:
            return err
        # 좌표를 주면 카카오가 거리순으로 정렬해 준다.
        params |= {"x": anchor["x"], "y": anchor["y"], "sort": "distance"}

    data, err = await _get(SEARCH_URL, params)
    if err:
        return err

    documents = data.get("documents") or []
    if not documents:
        return f"'{query}' 을(를) 찾지 못했습니다."

    results = []
    for place in documents:
        item = {
            "name": place.get("place_name", ""),
            "address": place.get("road_address_name") or place.get("address_name", ""),
        }
        if phone := place.get("phone"):
            item["phone"] = phone
        if (distance := place.get("distance")) and distance.isdigit():
            item["distance"] = _km(int(distance))
        results.append(item)
    return results


@tool
async def get_directions(destination: str, origin: str = "", priority: str = "recommend") -> str:
    """차로 얼마나 걸리는지 알아본다. 소요시간·거리·통행료를 돌려준다.

    운전 중에 쓰는 도구라 답이 짧다. 지도를 띄워야 하면 돌려준 주소를
    open_android_app 으로 폰에서 열면 된다.

    Args:
        destination: 목적지 (예: "제주공항", "강남역 스타벅스").
        origin: 출발지. 비우면 설정된 집 주소에서 출발하는 것으로 본다.
        priority: recommend(추천) / time(최단시간) / distance(최단거리).
    """
    start_query = origin.strip() or settings.home_address
    if not start_query:
        return "어디서 출발하는지 알려주세요. (.env 의 HOME_ADDRESS 를 채워 두면 기본값으로 씁니다.)"

    start, err = await _geocode(start_query)
    if err:
        return err
    end, err = await _geocode(destination)
    if err:
        return err

    data, err = await _get(
        DIRECTIONS_URL,
        {
            "origin": f"{start['x']},{start['y']}",
            "destination": f"{end['x']},{end['y']}",
            "priority": _PRIORITY.get(priority.strip().lower(), "RECOMMEND"),
            "alternatives": "true",
            "summary": "true",  # 경로 좌표는 필요 없다. 요약만 받는다.
        },
    )
    if err:
        return err

    routes = [r for r in (data.get("routes") or []) if r.get("result_code") == 0]
    if not routes:
        failed = (data.get("routes") or [{}])[0]
        reason = failed.get("result_msg") or "경로를 찾지 못했습니다"
        return f"{start['name']} → {end['name']}: {reason}."

    lines = [f"{start['name']} → {end['name']}"]
    for route in routes[:3]:
        summary = route.get("summary") or {}
        parts = [_minutes(int(summary.get("duration", 0))), _km(int(summary.get("distance", 0)))]
        fare = summary.get("fare") or {}
        if toll := fare.get("toll"):
            parts.append(f"통행료 {toll:,}원")
        lines.append("- " + ", ".join(parts))

    # 폰에서 바로 길안내를 띄울 수 있는 주소. 좌표는 위도,경도 순이다.
    lines.append(f"길안내: https://map.kakao.com/link/to/{end['name']},{end['y']},{end['x']}")
    return "\n".join(lines)
