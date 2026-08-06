"""시켜 놓고 자리를 뜨는 작업.

"카이트 타고 올 동안 아마존에서 인기 상품 좀 모아 둬" — 자비스에게 시키는 일 중에는
몇 분씩 걸리는 게 있다. 지금처럼 응답을 기다리는 구조에서는 그런 걸 시킬 수가 없다.
말을 걸어 두면 즉시 돌아오고, 다 되면 능동 알림으로 결과를 밀어 준다.

정기 작업(scheduler)이 '정해진 시각에 반복'이라면 이쪽은 '지금 시작해서 언젠가 끝나는'
한 번짜리다. 승인해 줄 사람이 없는 자리에서 도는 건 같아서, 그래프를 태우는 부분은
headless.run_prompt 를 함께 쓴다.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.api.notifications import manager
from app.db.models import BackgroundTask
from app.db.session import async_session
from app.headless import run_prompt

log = logging.getLogger("javis.background")

# 한 건이 물릴 수 있는 시간. 크롤링·검색을 여러 번 도는 작업이라 정기 작업보다 넉넉히 준다.
TASK_TIMEOUT = 900.0

# 태스크 참조를 들고 있지 않으면 GC 가 걷어 간다(nodes._bg_tasks 와 같은 이유).
_running: set[asyncio.Task] = set()


async def _finish(task_id: int, status: str, result: str) -> None:
    async with async_session() as session:
        await session.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id)
            .values(status=status, result=result, finished_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def _worker(task_id: int, request: str) -> None:
    outcome = await run_prompt(request, thread_prefix=f"background-{task_id}", timeout=TASK_TIMEOUT)
    await _finish(task_id, "done" if outcome.ok else "failed", outcome.text)

    mark = "✅" if outcome.ok else "⚠️"
    await manager.broadcast(
        {"type": "proactive", "content": f"{mark} 부탁하신 작업이 끝났습니다: {request}\n\n{outcome.text}"}
    )


async def start(request: str) -> int:
    """작업을 걸어 두고 id 를 돌려준다. 여기서 기다리지 않는다."""
    async with async_session() as session:
        row = BackgroundTask(request=request, status="running")
        session.add(row)
        await session.commit()
        task_id = row.id

    task = asyncio.create_task(_worker(task_id, request))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task_id


async def listing(limit: int = 10) -> list[dict]:
    """최근 작업들. 진행 중인 것이 먼저 보이게 최신순으로."""
    async with async_session() as session:
        rows = (
            await session.execute(
                select(BackgroundTask).order_by(BackgroundTask.created_at.desc()).limit(limit)
            )
        ).scalars().all()

    return [
        {
            "id": r.id,
            "request": r.request,
            "status": r.status,
            "result": r.result,
            "started": r.created_at.isoformat() if r.created_at else "",
            "finished": r.finished_at.isoformat() if r.finished_at else "",
        }
        for r in rows
    ]


async def clear_stale() -> int:
    """기동할 때 남아 있는 running 을 정리한다.

    프로세스가 죽으면 진행 중이던 작업은 살릴 수 없다. 그대로 두면 영원히 '진행 중'으로
    남아, 사용자는 오지 않을 결과를 계속 기다리게 된다.
    """
    async with async_session() as session:
        result = await session.execute(
            update(BackgroundTask)
            .where(BackgroundTask.status == "running")
            .values(
                status="interrupted",
                result="서버가 다시 시작되어 중단됐습니다. 필요하면 다시 시켜 주세요.",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    count = result.rowcount or 0
    if count:
        log.info("중단된 백그라운드 작업 %d건 정리", count)
    return count
