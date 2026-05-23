import logging

from sqlalchemy import select

from app.db.models import MemoryItem
from app.db.session import async_session
from app.llm import embeddings

log = logging.getLogger("javis.memory")


class LongTermMemory:
    """pgvector 기반 장기 기억.

    모든 대화를 그대로 쌓으면 검색 노이즈가 폭증한다. 반추 단계에서 추려낸
    조각만 들어오는 걸 전제로 한다.
    """

    async def embed(self, text: str) -> list[float]:
        return await embeddings().aembed_query(text)

    async def save(self, content: str, category: str = "general", importance: int = 5) -> None:
        content = content.strip()
        if not content:
            return
        vec = await self.embed(content)
        async with async_session() as session:
            session.add(
                MemoryItem(
                    content=content,
                    category=category,
                    importance=importance,
                    embedding=vec,
                )
            )
            await session.commit()

    async def retrieve(self, query: str, top_k: int = 5) -> list[str]:
        query = query.strip()
        if not query:
            return []
        vec = await self.embed(query)
        async with async_session() as session:
            stmt = (
                select(MemoryItem)
                .order_by(MemoryItem.embedding.l2_distance(vec))
                .limit(top_k)
            )
            rows = await session.execute(stmt)
            return [row.content for row in rows.scalars()]


long_term = LongTermMemory()
