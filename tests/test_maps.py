"""지도 도구.

운전 중에 음성으로 듣는 게 목적이라, 형식(분·킬로미터 표기)과 실패 처리가
정확도만큼 중요하다. 카카오 호출은 _get 을 갈아끼워 가로챈다.
"""

import pytest

from app.tools import maps

PLACE = {
    "documents": [
        {
            "place_name": "제주국제공항",
            "road_address_name": "제주 제주시 공항로 2",
            "x": "126.4930",
            "y": "33.5070",
            "phone": "1661-2626",
            "distance": "12300",
        }
    ]
}

ROUTE = {
    "routes": [
        {
            "result_code": 0,
            "summary": {"distance": 12300, "duration": 1320, "fare": {"toll": 1800, "taxi": 15000}},
        },
        {
            "result_code": 0,
            "summary": {"distance": 15000, "duration": 1500, "fare": {"toll": 0}},
        },
    ]
}


@pytest.fixture
def kakao(monkeypatch):
    """호출된 URL·파라미터를 모으고, URL 별로 정해진 응답을 돌려준다."""
    calls: list[tuple[str, dict]] = []
    replies = {maps.SEARCH_URL: PLACE, maps.DIRECTIONS_URL: ROUTE}

    async def fake_get(url, params):
        calls.append((url, params))
        return replies.get(url), None

    monkeypatch.setattr(maps.settings, "kakao_rest_api_key", "test-key")
    monkeypatch.setattr(maps.settings, "home_address", "")
    monkeypatch.setattr(maps, "_get", fake_get)
    return calls, replies


# --- 표기 ---


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0분"), (59, "1분"), (1320, "22분"), (3600, "1시간"), (5400, "1시간 30분")],
)
def test_duration_reads_naturally(seconds, expected):
    assert maps._minutes(seconds) == expected


@pytest.mark.parametrize("meters,expected", [(800, "800m"), (1000, "1.0km"), (12300, "12.3km")])
def test_distance_reads_naturally(meters, expected):
    assert maps._km(meters) == expected


@pytest.mark.parametrize(
    "spoken,expected",
    [("추천", "RECOMMEND"), ("빠른", "TIME"), ("최단거리", "DISTANCE"), ("아무말", "RECOMMEND")],
)
def test_priority_accepts_korean(kakao, spoken, expected):
    assert maps._PRIORITY.get(spoken, "RECOMMEND") == expected


# --- 설정 없음 ---


async def test_without_api_key_returns_hint(monkeypatch):
    monkeypatch.setattr(maps.settings, "kakao_rest_api_key", "")
    assert await maps.search_place.ainvoke({"query": "공항"}) == maps.SETUP_HINT


async def test_directions_without_origin_asks(kakao):
    result = await maps.get_directions.ainvoke({"destination": "제주공항"})
    assert "어디서 출발" in result


# --- 장소 검색 ---


async def test_search_returns_readable_fields(kakao):
    result = await maps.search_place.ainvoke({"query": "공항"})
    assert result == [
        {
            "name": "제주국제공항",
            "address": "제주 제주시 공항로 2",
            "phone": "1661-2626",
            "distance": "12.3km",
        }
    ]


async def test_search_sorts_by_distance_when_anchored(kakao):
    calls, _ = kakao
    await maps.search_place.ainvoke({"query": "맥도날드", "near": "제주공항"})

    # 첫 호출은 기준점 좌표를 얻는 것, 두 번째가 실제 검색이다.
    _, params = calls[-1]
    assert params["sort"] == "distance"
    assert params["x"] == "126.4930" and params["y"] == "33.5070"


async def test_home_address_is_the_default_anchor(kakao, monkeypatch):
    calls, _ = kakao
    monkeypatch.setattr(maps.settings, "home_address", "제주시청")
    await maps.search_place.ainvoke({"query": "맥도날드"})
    assert calls[-1][1]["sort"] == "distance"


async def test_search_reports_nothing_found(kakao, monkeypatch):
    async def empty(url, params):
        return {"documents": []}, None

    monkeypatch.setattr(maps, "_get", empty)
    assert "찾지 못했" in await maps.search_place.ainvoke({"query": "없는가게"})


# --- 길찾기 ---


async def test_directions_summarize_each_route(kakao):
    result = await maps.get_directions.ainvoke(
        {"destination": "제주공항", "origin": "제주시청"}
    )

    assert "제주국제공항" in result
    assert "22분, 12.3km, 통행료 1,800원" in result
    assert "25분, 15.0km" in result


async def test_directions_omit_zero_toll(kakao):
    result = await maps.get_directions.ainvoke({"destination": "제주공항", "origin": "제주시청"})
    second = [line for line in result.splitlines() if "25분" in line][0]
    assert "통행료" not in second


async def test_directions_include_a_phone_link(kakao):
    result = await maps.get_directions.ainvoke({"destination": "제주공항", "origin": "제주시청"})
    # 카카오 링크는 위도,경도 순이다. 뒤집으면 엉뚱한 곳으로 안내한다.
    assert "https://map.kakao.com/link/to/제주국제공항,33.5070,126.4930" in result


async def test_directions_ask_for_summary_only(kakao):
    """경로 폴리라인까지 받으면 응답이 수십 KB 가 된다. 소요시간만 있으면 된다."""
    calls, _ = kakao
    await maps.get_directions.ainvoke({"destination": "제주공항", "origin": "제주시청"})
    assert calls[-1][1]["summary"] == "true"


async def test_failed_route_reports_the_reason(kakao, monkeypatch):
    async def fail(url, params):
        if url == maps.SEARCH_URL:
            return PLACE, None
        return {"routes": [{"result_code": 104, "result_msg": "출발지와 도착지가 너무 가까움"}]}, None

    monkeypatch.setattr(maps, "_get", fail)
    result = await maps.get_directions.ainvoke({"destination": "옆집", "origin": "우리집"})
    assert "너무 가까움" in result


async def test_unknown_destination_stops_before_routing(kakao, monkeypatch):
    calls, _ = kakao

    async def no_place(url, params):
        return {"documents": []}, None

    monkeypatch.setattr(maps, "_get", no_place)
    result = await maps.get_directions.ainvoke({"destination": "없는곳", "origin": "제주시청"})
    assert "찾지 못했" in result
    assert all(url != maps.DIRECTIONS_URL for url, _ in calls)
