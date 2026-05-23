from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.tools import tool
from sqlalchemy import select

from app.config import settings
from app.db.models import Reminder
from app.db.session import async_session


@tool
async def create_reminder(content: str, due_iso: str = "") -> str:
    """리마인더를 등록한다. due_iso(ISO 8601)를 주면 그 시각에 알림이 온다.

    Args:
        content: 기억시킬 내용.
        due_iso: 알림 시각 (예: 2026-05-25T09:00:00+09:00). 비우면 시간 없는 할 일.
    """
    due = None
    if due_iso:
        try:
            due = datetime.fromisoformat(due_iso)
        except ValueError:
            return "due_iso 형식이 올바르지 않습니다. 예: 2026-05-25T09:00:00+09:00"
        # 오프셋이 없으면 기본 타임존으로 간주 (스케줄러는 tz-aware 로 비교한다).
        if due.tzinfo is None:
            due = due.replace(tzinfo=ZoneInfo(settings.timezone))

    async with async_session() as session:
        reminder = Reminder(content=content, due_at=due)
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)
        suffix = f" ({due_iso})" if due else ""
        return f"리마인더 #{reminder.id} 등록: {content}{suffix}"


@tool
async def list_reminders(include_done: bool = False) -> list[dict]:
    """리마인더 목록을 본다. 기본은 아직 안 끝난 것만.

    Args:
        include_done: True 면 완료된 것도 포함.
    """
    async with async_session() as session:
        stmt = select(Reminder).order_by(Reminder.due_at.is_(None), Reminder.due_at.asc())
        if not include_done:
            stmt = stmt.where(Reminder.done.is_(False))
        rows = await session.execute(stmt)
        return [
            {
                "id": r.id,
                "content": r.content,
                "due_at": r.due_at.isoformat() if r.due_at else None,
                "done": r.done,
            }
            for r in rows.scalars()
        ]


@tool
async def complete_reminder(reminder_id: int) -> str:
    """리마인더를 완료 처리한다.

    Args:
        reminder_id: 완료할 리마인더 번호.
    """
    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None:
            return f"리마인더 #{reminder_id} 를 찾을 수 없습니다."
        reminder.done = True
        await session.commit()
        return f"리마인더 #{reminder_id} 완료 처리했습니다."
