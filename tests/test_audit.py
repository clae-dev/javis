"""감사 로그 쓰기.

한 턴에 도구가 여럿 돌면 기록도 그만큼 생긴다. 건마다 커밋하면 응답 경로 밖의
일인데도 커넥션 풀을 갉아먹어, 정작 도구가 쓸 커넥션이 모자라진다. 눈에 보이는
증상이 '가끔 응답이 느리다' 뿐이라 원인을 짚기가 어렵다 — 그래서 여기 못박는다.
"""

import asyncio

import pytest

from app.db import audit


def _session_class(commits: list[list]):
    """커밋 묶음을 그대로 모아 두는 가짜 세션."""

    class _Session:
        def __init__(self) -> None:
            self.rows: list = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add_all(self, rows):
            self.rows.extend(rows)

        async def commit(self):
            commits.append(list(self.rows))

    return _Session


@pytest.fixture
def writes(monkeypatch):
    """세션을 메모리로 바꾸고, 모듈 전역 큐·writer 를 테스트마다 새로 시작한다."""
    commits: list[list] = []
    monkeypatch.setattr(audit, "async_session", _session_class(commits))
    monkeypatch.setattr(audit, "_LINGER", 0.01)
    monkeypatch.setattr(audit, "_queue", None)
    monkeypatch.setattr(audit, "_writer", None)
    return commits


async def test_a_turns_records_land_in_one_commit(writes):
    for name in ("get_time", "web_search", "recall"):
        await audit.record("tool", name)
    await audit.flush()

    assert len(writes) == 1
    assert [row.name for row in writes[0]] == ["get_time", "web_search", "recall"]


async def test_record_does_not_wait_for_the_write(writes):
    """기록이 본 흐름을 붙잡으면 도구 하나가 끝날 때마다 DB 왕복이 붙는다."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    await audit.record("tool", "get_time")
    assert loop.time() - start < audit._LINGER

    await audit.flush()
    assert writes


async def test_a_failed_write_does_not_wedge_the_queue(writes, monkeypatch):
    """DB 가 잠깐 안 붙어도 다음 기록은 계속 들어가야 한다."""

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("DB 없음")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(audit, "async_session", _Broken)
    await audit.record("tool", "get_time")
    await audit.flush()   # 못 써도 여기서 멈추지 않는다
    assert not writes

    monkeypatch.setattr(audit, "async_session", _session_class(writes))
    await audit.record("tool", "recall")
    await audit.flush()
    assert [row.name for commit in writes for row in commit] == ["recall"]
