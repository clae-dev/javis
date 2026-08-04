"""정기 작업.

"매일 아침 8시에 부동산 새 매물 확인해줘" 같은 요청을 받아 두고, 그때가 되면 실제로
알아본 뒤 알림으로 띄운다. 리마인더가 '적어 둔 문장을 그 시각에 읽어 주는' 것이라면
이쪽은 '그 시각에 가서 직접 해 보는' 것이다.
"""

import re

from langchain_core.tools import tool
from sqlalchemy import select

from app import scheduler
from app.db.models import ScheduledJob
from app.db.session import async_session

_NAME_OK = re.compile(r"^[\w가-힣 .-]{1,64}$")

# 사람 말로 자주 나오는 주기를 cron 으로 옮겨 둔다. 모델이 cron 을 직접 짜도 되지만,
# 흔한 것만이라도 정확히 맞춰 두면 엉뚱한 시각에 도는 일이 준다.
_PRESETS = {
    "매일아침": "0 8 * * *",
    "매일저녁": "0 20 * * *",
    "매시간": "0 * * * *",
    "평일아침": "0 8 * * 1-5",
    "매주월요일": "0 9 * * 1",
}


@tool
async def create_schedule(name: str, cron: str, prompt: str) -> str:
    """정기적으로 반복할 작업을 등록한다. 실행 전 사용자 확인을 거친다.

    등록해 두면 그 시각마다 prompt 를 실제로 수행하고 결과를 알림으로 보낸다.
    한 번만 알려 주면 되는 일은 이게 아니라 create_reminder 를 쓴다.

    Args:
        name: 작업 이름. 나중에 지울 때 쓴다 (예: "아침 매물 확인").
        cron: 표준 5필드 cron. 분 시 일 월 요일 순.
            매일 아침 8시 → "0 8 * * *" / 평일 9시 → "0 9 * * 1-5" / 매시 정각 → "0 * * * *"
        prompt: 그 시각에 자비스가 수행할 내용. 사람에게 시키듯 한 문장으로 쓴다.
    """
    name = name.strip()
    if not _NAME_OK.match(name):
        return "작업 이름은 64자 이내의 한글·영문·숫자로 지어 주세요."
    if not prompt.strip():
        return "그 시각에 무엇을 할지 알려주세요."

    cron = _PRESETS.get(cron.strip().replace(" ", ""), cron.strip())
    if err := scheduler.validate_cron(cron):
        return err

    async with async_session() as session:
        existing = (
            await session.execute(select(ScheduledJob).where(ScheduledJob.name == name))
        ).scalar_one_or_none()

        if existing is not None:
            existing.cron, existing.prompt, existing.enabled = cron, prompt.strip(), True
            job = existing
            verb = "수정"
        else:
            job = ScheduledJob(name=name, cron=cron, prompt=prompt.strip())
            session.add(job)
            verb = "등록"

        await session.commit()
        await session.refresh(job)

    if err := scheduler.add_job(job):
        return f"저장은 했지만 예약을 걸지 못했습니다: {err}"
    return f"'{name}' {verb}했습니다. ({cron})"


@tool
async def list_schedules() -> list[dict] | str:
    """등록해 둔 정기 작업 목록을 본다."""
    async with async_session() as session:
        rows = (
            await session.execute(select(ScheduledJob).order_by(ScheduledJob.created_at))
        ).scalars().all()

    if not rows:
        return "등록된 정기 작업이 없습니다."
    return [
        {
            "name": job.name,
            "cron": job.cron,
            "prompt": job.prompt,
            "enabled": job.enabled,
            "last_run": job.last_run_at.isoformat() if job.last_run_at else None,
        }
        for job in rows
    ]


@tool
async def delete_schedule(name: str) -> str:
    """등록해 둔 정기 작업을 지운다. 실행 전 사용자 확인을 거친다.

    Args:
        name: 지울 작업 이름. list_schedules 로 확인한 이름을 그대로 쓴다.
    """
    async with async_session() as session:
        job = (
            await session.execute(select(ScheduledJob).where(ScheduledJob.name == name.strip()))
        ).scalar_one_or_none()
        if job is None:
            return f"'{name}' 이라는 정기 작업이 없습니다."
        await session.delete(job)
        await session.commit()

    scheduler.remove_job(name.strip())
    return f"'{name}' 정기 작업을 지웠습니다."
