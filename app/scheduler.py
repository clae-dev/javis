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
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.api.notifications import manager
from app.config import settings
from app.db.models import Reminder, ScheduledJob
from app.db.session import async_session

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


# --- 사용자 정기 작업 ---


def _answer_of(state: dict) -> str:
    """그래프 최종 상태에서 마지막 답변 텍스트만 뽑는다."""
    from langchain_core.messages import AIMessage

    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and isinstance(message.content, str) and message.content.strip():
            return message.content.strip()
    return ""


async def _run_job(job_id: int) -> None:
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from app.agent.runtime import runtime

    async with async_session() as session:
        job = await session.get(ScheduledJob, job_id)
        if job is None or not job.enabled:
            return
        name, prompt = job.name, job.prompt

    if runtime.graph is None:
        log.warning("예약 작업 '%s' 건너뜀 — 그래프가 아직 준비되지 않았습니다.", name)
        return

    # 실행마다 대화 맥락을 새로 시작한다. 예약 작업은 이어 말하기가 아니라 매번 독립이고,
    # 앞선 실행에 확인 대기가 남아 있어도 다음 실행이 거기 물리지 않는다.
    thread = f"schedule-{name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    config = {"configurable": {"thread_id": thread}}
    payload = {
        "messages": [HumanMessage(content=prompt)],
        "user_profile": {"name": settings.owner_name},
    }

    notice = ""
    try:
        state = await asyncio.wait_for(
            runtime.graph.ainvoke(payload, config=config), timeout=JOB_TIMEOUT
        )
        if "__interrupt__" in state:
            blocked = state["__interrupt__"][0].value or {}
            names = ", ".join(a.get("name", "?") for a in blocked.get("actions", []))
            notice = f"\n\n(확인이 필요한 작업이라 실행하지 않았습니다: {names})"
            state = await asyncio.wait_for(
                runtime.graph.ainvoke(Command(resume=False), config=config), timeout=JOB_TIMEOUT
            )
        answer = _answer_of(state) or "(답변이 비었습니다)"
    except asyncio.TimeoutError:
        answer = "작업이 제한 시간 안에 끝나지 않았습니다."
    except Exception as exc:
        log.exception("예약 작업 실패: %s", name)
        answer = f"작업 중 오류가 났습니다: {exc}"

    await manager.broadcast({"type": "proactive", "content": f"🗓 {name}\n{answer}{notice}"})

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
