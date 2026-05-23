from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

from app.memory.long_term import long_term


@tool
async def get_current_time(timezone: str = "Asia/Seoul") -> str:
    """현재 날짜와 시각을 반환한다.

    Args:
        timezone: IANA 타임존 이름. 기본값은 Asia/Seoul.
    """
    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        timezone = "Asia/Seoul"
    return now.strftime(f"%Y-%m-%d %H:%M:%S ({timezone}, %A)")


@tool
async def remember(content: str, category: str = "general", importance: int = 5) -> str:
    """사용자에 대해 앞으로 기억해야 할 사실을 장기 기억에 저장한다.

    선호도, 약속, 반복되는 맥락처럼 다음 대화에서도 유용할 정보에만 쓴다.

    Args:
        content: 기억할 내용 (한 문장으로 명확하게).
        category: 분류 (예: preference, schedule, person, project).
        importance: 1~10. 높을수록 오래 보존.
    """
    await long_term.save(content=content, category=category, importance=importance)
    return f"기억했습니다: {content}"


@tool
async def recall(query: str, top_k: int = 5) -> list[str]:
    """장기 기억에서 질문과 관련된 내용을 검색한다.

    Args:
        query: 찾고 싶은 주제나 질문.
        top_k: 가져올 개수.
    """
    return await long_term.retrieve(query, top_k=top_k)
