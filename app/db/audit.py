"""감사 로그.

한 턴에 도구가 셋 넘게 돌면 기록도 그만큼 생긴다. 건마다 세션을 열고 커밋하면
응답 경로 밖의 일인데도 커넥션 풀을 그 수만큼 갉아먹는다 — 정작 도구가 쓸 커넥션이
모자라 응답이 대기로 직렬화되는 게 이 파일이 조용히 만들던 손해였다.

그래서 기록은 큐에 넣고 끝낸다. 뒤에서 도는 writer 가 잠깐(_LINGER) 더 모아 한
트랜잭션으로 밀어 넣는다. 같은 턴의 도구들은 거의 동시에 끝나므로, 이 짧은 기다림
하나로 대개 커밋 한 번에 담긴다.
"""

import asyncio
import logging

from app.db.models import AuditLog
from app.db.session import async_session

log = logging.getLogger("javis.audit")

# 첫 건이 들어온 뒤 이만큼 더 모은다. 같은 턴의 나머지 도구가 끝날 만한 시간.
_LINGER = 0.2
# 한 트랜잭션에 담을 최대 건수. 색인처럼 한꺼번에 쏟아지는 경우의 상한.
_BATCH_MAX = 50
# 큐가 이보다 밀리면 DB 가 죽은 것이다. 무한히 쌓아 메모리를 먹느니 버린다.
_QUEUE_MAX = 1000

_queue: asyncio.Queue | None = None
_writer: asyncio.Task | None = None


def _ensure_writer() -> asyncio.Queue:
    global _queue, _writer
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    if _writer is None or _writer.done():
        _writer = asyncio.create_task(_write_forever())
    return _queue


async def record(kind: str, name: str, request="", response="", ok: bool = True) -> None:
    """감사 로그를 남긴다. 실제 쓰기는 뒤에서 묶어서 한다.

    절대 본 흐름을 막지 않는다 — 큐에 넣는 것으로 끝이고, 실패는 삼킨다.
    """
    entry = AuditLog(
        kind=kind,
        name=name,
        request=str(request)[:8000],
        response=str(response)[:8000],
        ok=ok,
    )
    try:
        _ensure_writer().put_nowait(entry)
    except asyncio.QueueFull:
        log.warning("감사 로그가 밀려 이번 건은 버립니다: %s/%s", kind, name)
    except Exception as exc:
        log.debug("audit 기록 실패(무시): %s", exc)


async def flush(timeout: float = 5.0) -> None:
    """큐에 남은 기록을 마저 쓴다. 종료 직전에 부른다."""
    if _queue is None or _queue.empty():
        return
    try:
        await asyncio.wait_for(_queue.join(), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("감사 로그를 다 쓰지 못한 채 종료합니다 (남은 %d건)", _queue.qsize())


async def _write_forever() -> None:
    while True:
        try:
            await _write_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("audit 기록 실패(무시): %s", exc)


async def _collect() -> list[AuditLog]:
    """첫 건을 기다렸다가, 짧게 더 모아 한 묶음으로 돌려준다."""
    batch = [await _queue.get()]
    await asyncio.sleep(_LINGER)
    while len(batch) < _BATCH_MAX:
        try:
            batch.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


async def _write_batch() -> None:
    batch = await _collect()
    try:
        async with async_session() as session:
            session.add_all(batch)
            await session.commit()
    finally:
        # 실패해도 표시는 해야 한다. 안 그러면 flush() 가 영영 안 끝난다.
        for _ in batch:
            _queue.task_done()
