"""체크포인터 폴백 사슬.

여기가 조용히 인메모리로 떨어지면 재시작할 때마다 대화 맥락과 보류 중인 확인이
사라진다. 그런데 로그 한 줄 말고는 티가 안 나서, 몇 달을 모르고 쓸 수 있다.
실제로 그렇게 돌고 있었다 — 윈도우 호스트에서 psycopg 가 기본 이벤트 루프에
못 붙어서. 그래서 '한 단계만 물러나는지'를 여기서 못박아 둔다.
"""

from contextlib import AsyncExitStack

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent import graph
from app.agent.graph import make_checkpointer


@pytest.fixture
def sqlite_path(tmp_path, monkeypatch):
    path = tmp_path / "체크포인트" / "ckpt.sqlite"
    monkeypatch.setattr(graph.settings, "checkpoint_sqlite_path", str(path))
    return path


async def test_falls_back_to_sqlite_not_memory(sqlite_path, monkeypatch):
    """Postgres 를 못 쓰면 파일로 물러나야 한다. 메모리는 마지막 수단이다."""
    monkeypatch.setattr(graph.settings, "use_postgres_checkpointer", False)

    async with AsyncExitStack() as stack:
        saver = await make_checkpointer(stack)
        assert not isinstance(saver, MemorySaver)
        assert type(saver).__name__ == "AsyncSqliteSaver"


async def test_sqlite_file_is_actually_created(sqlite_path, monkeypatch):
    """폴더가 없어도 만들어야 한다 — credentials/ 가 비어 있는 새 클론에서도 떠야 한다."""
    monkeypatch.setattr(graph.settings, "use_postgres_checkpointer", False)
    assert not sqlite_path.exists()

    async with AsyncExitStack() as stack:
        await make_checkpointer(stack)

    assert sqlite_path.exists()


async def test_memory_is_the_last_resort(tmp_path, monkeypatch):
    """파일조차 못 쓰면 그때는 메모리로 간다. 뜨긴 떠야 한다."""
    monkeypatch.setattr(graph.settings, "use_postgres_checkpointer", False)
    # 파일을 폴더처럼 쓰게 만들어 sqlite 준비를 실패시킨다
    blocker = tmp_path / "파일"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(graph.settings, "checkpoint_sqlite_path", str(blocker / "ckpt.sqlite"))

    async with AsyncExitStack() as stack:
        assert isinstance(await make_checkpointer(stack), MemorySaver)


async def test_postgres_is_tried_first(sqlite_path, monkeypatch):
    """설정이 켜져 있으면 Postgres 를 먼저 시도해야 한다. 순서가 뒤집히면
    Docker 로 띄웠을 때도 파일로 새 버린다."""
    tried = []

    class _Boom:
        @staticmethod
        def from_conn_string(dsn):
            tried.append(dsn)
            raise RuntimeError("일부러 실패")

    monkeypatch.setattr(graph.settings, "use_postgres_checkpointer", True)
    monkeypatch.setitem(
        __import__("sys").modules,
        "langgraph.checkpoint.postgres.aio",
        type("m", (), {"AsyncPostgresSaver": _Boom}),
    )

    async with AsyncExitStack() as stack:
        saver = await make_checkpointer(stack)

    assert tried, "Postgres 를 시도조차 하지 않았다"
    assert type(saver).__name__ == "AsyncSqliteSaver"
