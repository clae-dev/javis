import logging
from collections import OrderedDict

from sqlalchemy import select

from app.db.models import MemoryItem
from app.db.session import async_session
from app.llm import embeddings

log = logging.getLogger("javis.memory")

# 매 턴 prepare 가 사용자 발화를 임베딩해 기억을 조회한다(임계 경로, OpenAI 왕복).
# "응"·"고마워"·"오늘 일정" 같은 짧거나 반복되는 발화는 같은 임베딩을 매번 새로 부른다.
# 임베딩은 입력+모델이 같으면 결정적이라, 최근 쿼리를 LRU 로 들고 재사용해 왕복을 없앤다.
# 동시 같은 쿼리가 둘 다 miss 해 둘 다 호출해도 결과가 같아 무해하므로 락은 두지 않는다.
_EMBED_CACHE_MAX = 256
_embed_cache: OrderedDict[str, list[float]] = OrderedDict()


class LongTermMemory:
    """pgvector 기반 장기 기억.

    모든 대화를 그대로 쌓으면 검색 노이즈가 폭증한다. 반추 단계에서 추려낸
    조각만 들어오는 걸 전제로 한다.
    """

    async def embed(self, text: str) -> list[float]:
        cached = _embed_cache.get(text)
        if cached is not None:
            _embed_cache.move_to_end(text)
            return cached
        vec = await embeddings().aembed_query(text)
        _embed_cache[text] = vec
        _embed_cache.move_to_end(text)
        if len(_embed_cache) > _EMBED_CACHE_MAX:
            _embed_cache.popitem(last=False)
        return vec

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

    async def save_many(self, facts: list[tuple[str, str, int]]) -> None:
        """여러 기억을 한 번에 저장한다.

        반추 단계는 보통 여러 조각을 한꺼번에 추려낸다. 조각마다 임베딩 호출 + 커밋을
        돌리면 왕복이 개수만큼 쌓이므로, 임베딩은 한 번에 묶어 요청하고 insert 는 한
        트랜잭션으로 커밋한다.
        """
        cleaned = [(c.strip(), cat, imp) for c, cat, imp in facts if c and c.strip()]
        if not cleaned:
            return
        vecs = await embeddings().aembed_documents([c for c, _, _ in cleaned])
        async with async_session() as session:
            session.add_all(
                [
                    MemoryItem(content=c, category=cat, importance=imp, embedding=vec)
                    for (c, cat, imp), vec in zip(cleaned, vecs)
                ]
            )
            await session.commit()

    async def retrieve(self, query: str, top_k: int = 5) -> list[str]:
        query = query.strip()
        if not query:
            return []
        vec = await self.embed(query)
        async with async_session() as session:
            # content 만 쓰는데 행 전체를 잡으면 embedding(1536-float 벡터)까지 매 턴 전송·
            # 역직렬화한다. 거리는 ORDER BY 안에서 DB 가 계산하니 content 컬럼만 받으면 된다.
            stmt = (
                select(MemoryItem.content)
                .order_by(MemoryItem.embedding.l2_distance(vec))
                .limit(top_k)
            )
            rows = await session.execute(stmt)
            return list(rows.scalars())


long_term = LongTermMemory()
