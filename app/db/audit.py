import logging

from app.db.models import AuditLog
from app.db.session import async_session

log = logging.getLogger("javis.audit")


async def record(kind: str, name: str, request="", response="", ok: bool = True) -> None:
    """감사 로그 기록. 절대 본 흐름을 막지 않도록 실패는 삼킨다."""
    try:
        async with async_session() as session:
            session.add(
                AuditLog(
                    kind=kind,
                    name=name,
                    request=str(request)[:8000],
                    response=str(response)[:8000],
                    ok=ok,
                )
            )
            await session.commit()
    except Exception as exc:
        log.debug("audit 기록 실패(무시): %s", exc)
