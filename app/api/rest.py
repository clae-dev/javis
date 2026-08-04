from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import require_token
from app.config import settings
from app.db.models import MemoryItem
from app.db.session import async_session

router = APIRouter()

# /health 는 살아있는지만 보는 용도라 토큰 없이 열어 둔다.


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "assistant": settings.assistant_name, "openai": settings.has_openai}


@router.get("/memories", dependencies=[Depends(require_token)])
async def memories(limit: int = 50) -> list[dict]:
    """저장된 장기 기억을 최근순으로 본다. 디버깅·점검용."""
    async with async_session() as session:
        stmt = select(MemoryItem).order_by(MemoryItem.created_at.desc()).limit(limit)
        rows = await session.execute(stmt)
        return [
            {
                "id": m.id,
                "content": m.content,
                "category": m.category,
                "importance": m.importance,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows.scalars()
        ]
