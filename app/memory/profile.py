"""사용자 프로필(싱글톤). 자비스가 사용자를 '알아가는' 기억의 요약본."""

from sqlalchemy import select

from app.db.models import UserProfile
from app.db.session import async_session


async def load_summary() -> str:
    async with async_session() as session:
        row = await session.get(UserProfile, 1)
        return row.summary if row else ""


async def update_summary(summary: str) -> None:
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
