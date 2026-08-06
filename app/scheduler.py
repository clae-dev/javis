"""능동 알림 스케줄러.

세 갈래를 돈다.
- 마감된 리마인더 확인 (1분마다)
- 아침 브리핑 (8시)
- 사용자가 말로 걸어 둔 정기 작업 (DB 의 scheduled_jobs)

정기 작업은 저장해 둔 문장을 그냥 읽어 주는 게 아니라, 그때 가서 자비스에게 실제로
물어보고 그 답을 알림으로 띄운다. 다만 승인해 줄 사람이 없는 시간대에 도는 만큼,
쓰기 도구 확인이 걸리면 실행하지 않고 취소로 정리한 뒤 알리기만 한다.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.api.notifications import manager
from app.config import settings
from app.db.models import Reminder, ScheduledJob
from app.db.session import async_session
from app.headless import run_prompt

log = logging.getLogger("javis.scheduler")
_scheduler: AsyncIOScheduler | None = None

JOB_PREFIX = "user:"
# 예약 작업 한 건이 물릴 수 있는 시간. 웹 검색·크롤링까지 하면 몇 분 걸릴 수 있다.
JOB_TIMEOUT = 300.0


# --- 붙박이 작업 ---


async def _check_due_reminders() -> None:
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        stmt = select(Reminder).where(
            Reminder.done.is_(False),
            Reminder.notified_at.is_(None),
            Reminder.due_at.is_not(None),
            Reminder.due_at <= now,
        )
        due = (await session.execute(stmt)).scalars().all()
        for reminder in due:
            reminder.notified_at = now
            await manager.broadcast({"type": "proactive", "content": f"⏰ 리마인더: {reminder.content}"})
        if due:
            await session.commit()


async def _morning_briefing() -> None:
    try:
        from app.tools.calendar import get_upcoming_events

        events = await get_upcoming_events.ainvoke({"days": 1})
        if isinstance(events, list) and events:
            lines = "\n".join(f"- {e['summary']} ({e['start']})" for e in events)
            await manager.broadcast({"type": "proactive", "content": f"☀️ 오늘 일정\n{lines}"})
    except Exception as exc:
        log.debug("아침 브리핑 건너뜀: %s", exc)


async def _index_notes() -> None:
    """노트 폴더 증분 색인. 바뀐 게 없으면 stat 만 돌고 끝난다."""
    try:
        from app.memory.indexer import reindex

        log.info("노트 색인: %s", await reindex())
    except Exception as exc:
        log.warning("노트 색인 실패(무시): %s", exc)


# --- 사용자 정기 작업 ---


async def _run_job(job_id: int) -> None:
    async with async_session() as session:
        job = await session.get(ScheduledJob, job_id)
        if job is None or not job.enabled:
            return
        name, prompt = job.name, job.prompt

    # 승인해 줄 사람이 없는 시간대에 도는 만큼, 쓰기 도구 확인이 걸리면 실행하지 않고
    # 취소로 정리한 뒤 알리기만 한다. 그 처리는 headless 가 맡는다.
    result = await run_prompt(prompt, thread_prefix=f"schedule-{name}", timeout=JOB_TIMEOUT)

    await manager.broadcast({"type": "proactive", "content": f"🗓 {name}\n{result.text}"})

    async with async_session() as session:
        job = await session.get(ScheduledJob, job_id)
        if job is not None:
            job.last_run_at = datetime.now(timezone.utc)
            await session.commit()


def add_job(job: ScheduledJob) -> str | None:
    """살아 있는 스케줄러에 작업을 건다. 문제가 있으면 사유를 돌려준다."""
    if _scheduler is None:
        return None  # 스케줄러가 꺼져 있어도 DB 에는 남는다. 다음 기동 때 붙는다.
    try:
        trigger = CronTrigger.from_crontab(job.cron, timezone=settings.timezone)
    except ValueError as exc:
        return f"cron 형식이 올바르지 않습니다: {exc}"
    _scheduler.add_job(
        _run_job,
        trigger,
        args=[job.id],
        id=f"{JOB_PREFIX}{job.name}",
        replace_existing=True,
        misfire_grace_time=300,
    )
    return None


def remove_job(name: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f"{JOB_PREFIX}{name}")
    except Exception:
        pass  # 이미 없으면 그만이다


def validate_cron(cron: str) -> str | None:
    """cron 문자열을 미리 검사한다. 문제가 있으면 사유, 없으면 None."""
    try:
        CronTrigger.from_crontab(cron, timezone=settings.timezone)
    except ValueError as exc:
        return f"cron 형식이 올바르지 않습니다: {exc}"
    return None


async def _load_jobs() -> None:
    async with async_session() as session:
        rows = (
            await session.execute(select(ScheduledJob).where(ScheduledJob.enabled.is_(True)))
        ).scalars().all()

    for job in rows:
        if err := add_job(job):
            log.warning("예약 작업 '%s' 를 걸지 못했습니다: %s", job.name, err)
    if rows:
        log.info("예약 작업 %d건 복원", len(rows))


# --- 수명 ---


async def start() -> None:
    global _scheduler
    if not settings.enable_scheduler:
        log.info("scheduler 비활성화")
        return
    _scheduler = AsyncIOScheduler(timezone=settings.timezone)
    _scheduler.add_job(_check_due_reminders, "interval", seconds=60, id="due_reminders")
    _scheduler.add_job(_morning_briefing, "cron", hour=8, minute=0, id="morning_briefing")
    if settings.notes_path:
        # 기동 직후 한 번 훑고(밖에서 고친 노트를 바로 반영) 이후 주기적으로 돈다.
        _scheduler.add_job(
            _index_notes,
            "interval",
            minutes=settings.notes_index_minutes,
            id="index_notes",
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
        )
    _scheduler.start()
    try:
        await _load_jobs()
    except Exception as exc:
        # DB 가 잠깐 안 붙어도 붙박이 알림은 계속 돌아야 한다.
        log.warning("예약 작업 복원 실패(무시): %s", exc)
    log.info("scheduler 시작")


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
