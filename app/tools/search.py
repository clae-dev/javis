import asyncio

from langchain_core.tools import tool

from app.config import settings


def _tavily(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(api_key=settings.tavily_api_key)
    data = client.search(query=query, max_results=max_results)
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])
    ]


def _ddg(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    with DDGS() as ddgs:
        results = ddgs.text(query, max_results=max_results)
    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in results
    ]


@tool
async def web_search(query: str, max_results: int = 5) -> list[dict] | str:
    """웹을 검색해 제목·요약·링크를 돌려준다. 최신 정보나 모르는 사실이 필요할 때 쓴다.

    Args:
        query: 검색어.
        max_results: 결과 개수 (기본 5, 최대 10).
    """
    max_results = max(1, min(max_results, 10))

    # Tavily 키가 있으면 우선 사용, 실패하면 ddgs 로 폴백.
    if settings.tavily_api_key:
        try:
            return await asyncio.to_thread(_tavily, query, max_results)
        except Exception:
            pass
    try:
        return await asyncio.to_thread(_ddg, query, max_results)
    except Exception as exc:
        return f"검색에 실패했습니다: {exc}"
