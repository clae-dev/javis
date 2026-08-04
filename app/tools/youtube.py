"""유튜브 데이터 API.

영상을 찾고, 조회수·좋아요를 읽고, 채널의 최근 영상을 훑는다. "요즘 이런 영상이 왜
잘 되지" 같은 질문에 실제 숫자로 답하려고 만들었다.

구글 OAuth 스코프는 건드리지 않는다 — 공개 데이터만 보므로 API 키 하나면 된다.
캘린더·Gmail 토큰을 다시 발급받을 필요가 없다.
"""

import asyncio
import re
import threading

from langchain_core.tools import tool

from app.config import settings

SETUP_HINT = (
    "유튜브 연결이 안 되어 있습니다. Google Cloud Console 에서 YouTube Data API v3 를 켜고 "
    "API 키를 만들어 .env 의 YOUTUBE_API_KEY 에 넣어 주세요."
)

_local = threading.local()

# watch?v=, youtu.be/, /shorts/, /embed/ 를 모두 받는다.
_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _service():
    """API 키 기반 서비스. httplib2 전송이 스레드 비안전이라 스레드별로 캐시한다."""
    service = getattr(_local, "youtube", None)
    if service is None:
        from googleapiclient.discovery import build

        service = _local.youtube = build(
            "youtube", "v3", developerKey=settings.youtube_api_key, cache_discovery=False
        )
    return service


def _video_id(value: str) -> str | None:
    value = value.strip()
    if m := _VIDEO_ID.search(value):
        return m.group(1)
    return value if _BARE_ID.match(value) else None


def _duration(iso: str) -> str:
    m = _DURATION.match(iso or "")
    if not m:
        return iso or ""
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    hours += days * 24
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- 동기 호출 (to_thread 로 감싸 쓴다) ---


def _search(query: str, max_results: int, order: str) -> list[dict]:
    res = (
        _service()
        .search()
        .list(q=query, part="snippet", type="video", maxResults=max_results, order=order)
        .execute()
    )
    return [
        {
            "title": (i["snippet"]).get("title", ""),
            "channel": (i["snippet"]).get("channelTitle", ""),
            "published": (i["snippet"]).get("publishedAt", "")[:10],
            "url": f"https://www.youtube.com/watch?v={i['id']['videoId']}",
        }
        for i in res.get("items", [])
        if i.get("id", {}).get("videoId")
    ]


def _videos(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    res = (
        _service()
        .videos()
        .list(id=",".join(ids), part="snippet,statistics,contentDetails")
        .execute()
    )
    out = []
    for item in res.get("items", []):
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        out.append(
            {
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "published": snippet.get("publishedAt", "")[:10],
                "duration": _duration(item.get("contentDetails", {}).get("duration", "")),
                "views": _int(stats.get("viewCount")),
                "likes": _int(stats.get("likeCount")),
                "comments": _int(stats.get("commentCount")),
                "url": f"https://www.youtube.com/watch?v={item['id']}",
            }
        )
    return out


def _channel_uploads(channel: str, max_results: int) -> list[dict]:
    youtube = _service()

    # 핸들(@name)이든 채널명이든 검색으로 채널 하나를 고른다.
    found = youtube.search().list(q=channel, part="snippet", type="channel", maxResults=1).execute()
    items = found.get("items", [])
    if not items:
        return []
    channel_id = items[0]["snippet"]["channelId"]

    detail = youtube.channels().list(id=channel_id, part="contentDetails").execute()
    channels = detail.get("items", [])
    if not channels:
        return []
    uploads = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    playlist = (
        youtube.playlistItems()
        .list(playlistId=uploads, part="contentDetails", maxResults=max_results)
        .execute()
    )
    ids = [i["contentDetails"]["videoId"] for i in playlist.get("items", [])]
    return _videos(ids)


async def _call(fn, *args):
    if not settings.youtube_api_key:
        return SETUP_HINT
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as exc:
        text = str(exc)
        if "quota" in text.lower():
            return "유튜브 API 하루 할당량을 다 썼습니다. 내일 다시 시도해 주세요."
        if "API key not valid" in text or "keyInvalid" in text:
            return SETUP_HINT
        return f"유튜브 조회에 실패했습니다: {text[:300]}"


# --- 도구 ---


@tool
async def search_youtube(query: str, max_results: int = 5, order: str = "relevance") -> list[dict] | str:
    """유튜브에서 영상을 찾는다.

    Args:
        query: 검색어.
        max_results: 결과 개수 (기본 5, 최대 25).
        order: relevance(기본) / date(최신순) / viewCount(조회수순) / rating.
    """
    max_results = max(1, min(max_results, 25))
    if order not in ("relevance", "date", "viewCount", "rating", "title"):
        order = "relevance"
    result = await _call(_search, query, max_results, order)
    if isinstance(result, str):
        return result
    return result or "검색 결과가 없습니다."


@tool
async def get_video_stats(video: str) -> dict | str:
    """유튜브 영상 하나의 조회수·좋아요·길이 같은 지표를 본다.

    Args:
        video: 영상 주소 또는 11자리 영상 ID.
    """
    vid = _video_id(video)
    if vid is None:
        return "영상 주소나 ID 를 알아보지 못했습니다."
    result = await _call(_videos, [vid])
    if isinstance(result, str):
        return result
    return result[0] if result else "그 영상을 찾지 못했습니다. 비공개이거나 삭제됐을 수 있습니다."


@tool
async def list_channel_videos(channel: str, max_results: int = 10) -> list[dict] | str:
    """채널의 최근 영상을 지표와 함께 훑는다. 어떤 영상이 잘 됐는지 볼 때 쓴다.

    Args:
        channel: 채널 이름이나 핸들 (예: "@DexterIT").
        max_results: 개수 (기본 10, 최대 25).
    """
    max_results = max(1, min(max_results, 25))
    result = await _call(_channel_uploads, channel, max_results)
    if isinstance(result, str):
        return result
    return result or f"'{channel}' 채널을 찾지 못했습니다."
