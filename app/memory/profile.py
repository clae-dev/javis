"""사용자 프로필(싱글톤). 자비스가 사용자를 '알아가는' 기억의 요약본."""

from app.db.models import UserProfile
from app.db.session import async_session

# 프로필은 매 턴 prepare 에서 읽지만 갱신은 반추(update_summary)에서만 일어난다. 단일
# 사용자·단일 프로세스라 인메모리로 들고, 쓰기 때 같이 갱신하는 write-through 로 일관성을
# 지킨다. 첫 로드 전엔 None 으로 두어 빈 프로필("")과 미로드를 구분한다.
_cache: str | None = None


async def load_summary() -> str:
    global _cache
    if _cache is not None:
        return _cache
    async with async_session() as session:
        row = await session.get(UserProfile, 1)
        _cache = row.summary if row else ""
        return _cache


async def update_summary(summary: str) -> None:
    global _cache
    summary = summary.strip()
    if not summary:
        return
    async with async_session() as session:
        row = await session.get(UserProfile, 1)
        if row is None:
            session.add(UserProfile(id=1, summary=summary))
        else:
            row.summary = summary
        await session.commit()
    _cache = summary
