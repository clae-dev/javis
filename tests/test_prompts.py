"""시스템 프롬프트의 캐시 접두사.

프롬프트 캐싱은 접두사 매칭이라, 앞쪽이 한 바이트라도 달라지면 그 뒤가 전부 무효가 된다.
시각이나 기분 같은 값이 다시 앞으로 올라오면 캐시가 통째로 죽는데, 눈으로는 안 보인다.
그래서 여기서 못박아 둔다.
"""

import os
from contextlib import contextmanager
from unittest.mock import patch

from app.agent import prompts
from app.agent.prompts import build_system_prompt

BASE = {"user_profile": {"name": "창래"}}


def _turn(**overrides) -> dict:
    return {**BASE, **overrides}


@contextmanager
def _clock(text: str):
    """프롬프트가 읽는 시계를 고정한다."""
    frozen = type("Frozen", (), {"strftime": lambda self, _fmt: text})()
    with patch.object(prompts, "datetime") as fake:
        fake.now.return_value = frozen
        yield


def test_stable_part_is_byte_identical_across_turns():
    """상태가 다 달라도 고정 구간은 똑같아야 한다."""
    a = build_system_prompt(_turn(mood="신남", retrieved_context=["기억 A"]))
    b = build_system_prompt(_turn(mood="지침", mode="drive", retrieved_context=["기억 B"]))

    stable = prompts._stable("창래")
    assert a.startswith(stable)
    assert b.startswith(stable)


def test_clock_change_does_not_shrink_the_cacheable_prefix():
    """시각이 바뀌어도 고정 구간 전체가 공유돼야 한다.

    이게 캐시가 먹히는지를 직접 재는 유일한 검사다. 시각이 다시 프롬프트 앞쪽으로
    올라가면 공통 접두사가 그 지점에서 잘려 나가고, 여기서 걸린다.
    """
    with _clock("2026-01-01 09:00 (Thursday)"):
        morning = build_system_prompt(_turn())
    with _clock("2027-12-31 23:59 (Friday)"):
        night = build_system_prompt(_turn())

    assert morning != night  # 시각 자체는 반영돼야 한다
    shared = os.path.commonprefix([morning, night])
    assert len(shared) >= len(prompts._stable("창래"))


def test_volatile_values_land_after_the_stable_part():
    prompt = build_system_prompt(
        _turn(mood="지치고 스트레스 받음", mode="drive", retrieved_context=["카이트서핑을 좋아한다"])
    )
    head = len(prompts._stable("창래"))

    for marker in ("[지금 시각]", "지치고 스트레스 받음", "[지금 운전 중]", "카이트서핑을 좋아한다"):
        assert prompt.index(marker) >= head, marker


def test_time_is_still_present():
    """캐시를 살리자고 시각을 통째로 빼면 안 된다."""
    assert "[지금 시각]" in build_system_prompt(_turn())


def test_neutral_mood_is_omitted():
    assert "[지금 창래 님 상태]" not in build_system_prompt(_turn(mood="중립"))


def test_owner_falls_back_to_settings():
    prompt = build_system_prompt({})
    assert prompts.settings.owner_name in prompt
