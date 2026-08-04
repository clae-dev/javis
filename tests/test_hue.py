"""휴 조명 — 이름 매칭, 색 해석, 요청 본문 구성.

브리지 없이 검증할 수 있는 부분만 본다. 실제 통신은 _request 를 갈아끼워 가로챈다.
"""

import pytest

from app.tools import hue

TARGETS = [
    {"kind": "light", "id": "l1", "name": "거실 스탠드", "on": True, "brightness": 80, "color": True},
    {"kind": "light", "id": "l2", "name": "안방 전등", "on": False, "brightness": 0, "color": False},
    {"kind": "room", "id": "g1", "name": "거실", "on": None, "brightness": None, "color": True},
]


@pytest.fixture
def captured(monkeypatch):
    """set_hue_light 이 브리지로 보내려던 요청을 모아 둔다."""
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_targets():
        return list(TARGETS), None

    async def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return [], None

    monkeypatch.setattr(hue, "_targets", fake_targets)
    monkeypatch.setattr(hue, "_request", fake_request)
    return calls


# --- 이름 매칭 ---


def test_exact_name_wins_over_partial():
    """'거실' 은 방 이름과 정확히 같다. '거실 스탠드'까지 같이 끄면 안 된다."""
    hits = hue._match("거실", TARGETS)
    assert [h["name"] for h in hits] == ["거실"]


def test_partial_name_matches():
    hits = hue._match("스탠드", TARGETS)
    assert [h["name"] for h in hits] == ["거실 스탠드"]


def test_spacing_is_ignored():
    """음성 인식이 띄어쓰기를 흘려도 찾아야 한다."""
    assert hue._match("거실스탠드", TARGETS)[0]["name"] == "거실 스탠드"


def test_all_keyword_selects_everything():
    assert len(hue._match("전체", TARGETS)) == len(TARGETS)


def test_unknown_name_matches_nothing():
    assert hue._match("베란다", TARGETS) == []


# --- 색 ---


@pytest.mark.parametrize("name", ["빨강", "빨간색", "레드", "RED", " red "])
def test_color_aliases_resolve_to_same_xy(name):
    assert hue._xy(name) == hue._COLORS["빨강"]


def test_unknown_color_is_none():
    assert hue._xy("형광연두") is None


# --- 요청 본문 ---


async def test_turn_off_sends_on_false(captured):
    result = await hue.set_hue_light.ainvoke({"name": "안방 전등", "on": False})
    assert captured == [("PUT", "/light/l2", {"on": {"on": False}})]
    assert "안방 전등" in result


async def test_room_uses_grouped_light_endpoint(captured):
    await hue.set_hue_light.ainvoke({"name": "거실", "on": True})
    assert captured[0][1] == "/grouped_light/g1"


async def test_brightness_zero_turns_off(captured):
    """'밝기 0' 은 끄라는 뜻이다. 브리지는 밝기 0을 받지 않는다."""
    await hue.set_hue_light.ainvoke({"name": "안방 전등", "brightness": 0})
    assert captured[0][2] == {"on": {"on": False}}


async def test_brightness_implies_on_and_is_clamped(captured):
    """꺼진 불의 밝기만 올리라고 하면 켜 달라는 뜻이고, 100을 넘기면 깎는다."""
    await hue.set_hue_light.ainvoke({"name": "안방 전등", "brightness": 150})
    body = captured[0][2]
    assert body["on"] == {"on": True}
    assert body["dimming"] == {"brightness": 100.0}


async def test_color_implies_on(captured):
    await hue.set_hue_light.ainvoke({"name": "거실 스탠드", "color": "파랑"})
    body = captured[0][2]
    assert body["on"] == {"on": True}
    assert body["color"]["xy"]["x"] == pytest.approx(hue._COLORS["파랑"][0])


async def test_unknown_color_does_not_touch_bridge(captured):
    result = await hue.set_hue_light.ainvoke({"name": "거실 스탠드", "color": "형광연두"})
    assert captured == []
    assert "형광연두" in result


async def test_no_change_requested_asks_back(captured):
    result = await hue.set_hue_light.ainvoke({"name": "거실 스탠드"})
    assert captured == []
    assert "무엇을" in result


async def test_unknown_light_lists_available_names(captured):
    result = await hue.set_hue_light.ainvoke({"name": "베란다", "on": True})
    assert captured == []
    assert "거실 스탠드" in result and "안방 전등" in result


async def test_setup_hint_when_key_missing(monkeypatch):
    monkeypatch.setattr(hue.settings, "hue_app_key", "")
    data, err = await hue._request("GET", "/light")
    assert data is None and "hue_auth" in err
