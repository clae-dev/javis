"""유튜브 도구 — 주소 해석과 길이 표기. API 키 없이 되는 부분만 본다."""

import pytest

from app.tools import youtube


@pytest.mark.parametrize(
    "value",
    [
        "https://www.youtube.com/watch?v=Ilt1DcM2nlk",
        "https://www.youtube.com/watch?v=Ilt1DcM2nlk&t=1s",
        "https://youtu.be/Ilt1DcM2nlk",
        "https://www.youtube.com/shorts/Ilt1DcM2nlk",
        "https://www.youtube.com/embed/Ilt1DcM2nlk",
        "Ilt1DcM2nlk",
    ],
)
def test_video_id_is_extracted(value):
    assert youtube._video_id(value) == "Ilt1DcM2nlk"


@pytest.mark.parametrize("value", ["https://example.com/watch", "그냥 문장", ""])
def test_non_video_returns_none(value):
    assert youtube._video_id(value) is None


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("PT35S", "0:35"),
        ("PT10M35S", "10:35"),
        ("PT1H2M3S", "1:02:03"),
        ("PT2H", "2:00:00"),
        ("P1DT1H", "25:00:00"),
    ],
)
def test_duration_is_readable(iso, expected):
    assert youtube._duration(iso) == expected


async def test_setup_hint_without_api_key(monkeypatch):
    monkeypatch.setattr(youtube.settings, "youtube_api_key", "")
    assert await youtube.search_youtube.ainvoke({"query": "자비스"}) == youtube.SETUP_HINT


async def test_bad_url_is_rejected_before_calling_api(monkeypatch):
    monkeypatch.setattr(youtube.settings, "youtube_api_key", "dummy")

    def explode(*_):
        raise AssertionError("API 를 부르면 안 된다")

    monkeypatch.setattr(youtube, "_videos", explode)
    result = await youtube.get_video_stats.ainvoke({"video": "이건 주소가 아니다"})
    assert "알아보지 못했" in result


async def test_order_falls_back_to_relevance(monkeypatch):
    monkeypatch.setattr(youtube.settings, "youtube_api_key", "dummy")
    seen = {}

    def fake_search(query, max_results, order):
        seen.update(query=query, max_results=max_results, order=order)
        return [{"title": "t"}]

    monkeypatch.setattr(youtube, "_search", fake_search)
    await youtube.search_youtube.ainvoke({"query": "q", "max_results": 99, "order": "이상한값"})
    assert seen == {"query": "q", "max_results": 25, "order": "relevance"}
