"""정기 작업 — cron 검증과 예약 실행의 안전장치.

DB 가 필요한 경로는 건드리지 않는다. 스케줄러 자체 동작(트리거 해석, 확인 대기 처리)만 본다.
"""

import pytest

from app import scheduler
from app.tools import schedules


@pytest.mark.parametrize("cron", ["0 8 * * *", "*/15 * * * *", "0 9 * * 1-5", "30 6 1 * *"])
def test_valid_cron_passes(cron):
    assert scheduler.validate_cron(cron) is None


@pytest.mark.parametrize("cron", ["매일 8시", "0 8 * *", "99 8 * * *", ""])
def test_invalid_cron_is_rejected(cron):
    assert scheduler.validate_cron(cron) is not None


def test_presets_are_all_valid_cron():
    """사람 말 → cron 표에 오타가 있으면 등록 시점에 터진다. 여기서 미리 잡는다."""
    for label, cron in schedules._PRESETS.items():
        assert scheduler.validate_cron(cron) is None, f"{label} → {cron}"


@pytest.mark.parametrize("name", ["아침 매물 확인", "daily-report", "주간_요약", "a" * 64])
def test_reasonable_names_pass(name):
    assert schedules._NAME_OK.match(name)


@pytest.mark.parametrize("name", ["", "a" * 65, "이름/슬래시", "탭\t포함"])
def test_bad_names_are_rejected(name):
    assert not schedules._NAME_OK.match(name)


async def test_create_rejects_bad_cron_before_touching_db(monkeypatch):
    def explode(*_, **__):
        raise AssertionError("DB 를 건드리면 안 된다")

    monkeypatch.setattr(schedules, "async_session", explode)
    result = await schedules.create_schedule.ainvoke(
        {"name": "테스트", "cron": "매일 여덟시", "prompt": "확인해줘"}
    )
    assert "cron" in result


async def test_create_rejects_empty_prompt(monkeypatch):
    def explode(*_, **__):
        raise AssertionError("DB 를 건드리면 안 된다")

    monkeypatch.setattr(schedules, "async_session", explode)
    result = await schedules.create_schedule.ainvoke(
        {"name": "테스트", "cron": "0 8 * * *", "prompt": "  "}
    )
    assert "무엇을" in result


def test_add_job_without_running_scheduler_is_quiet():
    """스케줄러가 꺼져 있어도 등록은 실패가 아니다. 다음 기동 때 DB 에서 복원된다."""
    scheduler._scheduler = None
    assert scheduler.add_job(_Row(name="x", cron="0 8 * * *", id=1)) is None


def test_remove_missing_job_does_not_raise():
    scheduler._scheduler = None
    scheduler.remove_job("없는작업")


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- 예약 실행의 안전장치 ---


def test_answer_of_picks_last_ai_message():
    from langchain_core.messages import AIMessage, HumanMessage

    state = {
        "messages": [
            HumanMessage(content="확인해줘"),
            AIMessage(content="", tool_calls=[]),
            AIMessage(content="새 매물 3건 있습니다."),
        ]
    }
    assert scheduler._answer_of(state) == "새 매물 3건 있습니다."


def test_answer_of_handles_empty_state():
    assert scheduler._answer_of({}) == ""
