import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

log = logging.getLogger("javis.db")

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """pgvector 확장과 테이블을 보장한다. 앱 시작 시 1회 호출."""
    from app.db.models import Base  # noqa: F401 (메타데이터 등록용)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    # 기억 검색은 embedding.l2_distance 로 정렬한다. 인덱스가 없으면 매 조회가 풀스캔이라
    # 기억이 쌓일수록 선형으로 느려진다. HNSW 로 근사 최근접을 태운다.
    # 구버전 pgvector(<0.5)엔 HNSW 가 없을 수 있어, 실패해도 부팅은 막지 않는다(풀스캔 폴백).
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_memory_items_embedding_hnsw "
                    "ON memory_items USING hnsw (embedding vector_l2_ops)"
                )
            )
    except Exception as exc:
        log.warning("HNSW 인덱스 생성 실패(검색은 풀스캔으로 동작): %s", exc)

    # 스케줄러가 1분마다 '아직 안 알린 마감된 리마인더'를 조회한다. 인덱스가 없으면 매번
    # 풀스캔이라 리마인더가 쌓일수록 느려진다. 조회 조건과 똑같은 부분 인덱스로 좁힌다.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_reminders_due ON reminders (due_at) "
                "WHERE done = false AND notified_at IS NULL"
            )
        )

    # 감사로그는 시간순 조회와 종류별 통계 조회가 빈번하다. 인덱스가 없으면 로그가
    # 쌓일수록 선형으로 느려진다.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_audit_log_kind_ok ON audit_log (kind, ok)"
            )
        )
