"""능동 알림 스케줄러.

마감된 리마인더를 1분마다 확인하고, 아침 8시에 오늘 일정을 브리핑한다.
알림은 연결된 모든 클라이언트로 push 된다.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.api.notifications import manager
from app.config import settings
from app.db.models import Reminder
from app.db.session import async_session

log = logging.getLogger("javis.scheduler")
_scheduler: AsyncIOScheduler | None = None


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


def start() -> None:
    global _scheduler
    if not settings.enable_scheduler:
        log.info("scheduler 비활성화")
        return
    _scheduler = AsyncIOScheduler(timezone=settings.timezone)
    _scheduler.add_job(_check_due_reminders, "interval", seconds=60, id="due_reminders")
    _scheduler.add_job(_morning_briefing, "cron", hour=8, minute=0, id="morning_briefing")
    _scheduler.start()
    log.info("scheduler 시작")


def stop() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
