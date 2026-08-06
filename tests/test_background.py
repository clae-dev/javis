"""맡겨 둔 작업의 수명.

시켜 놓고 자리를 뜨는 게 요점이라, 조용히 실패하는 방식이 두 가지 있다.
하나는 시작이 막혀 기다리게 되는 것, 다른 하나는 끝났는데 아무도 안 알려 주는 것.
그리고 서버가 죽으면 진행 중이던 건 살릴 수 없는데, 정리하지 않으면 영원히
'진행 중'으로 남아 오지 않을 결과를 계속 기다리게 된다.
"""

import asyncio

import pytest

from app import background
from app.headless import Outcome


class _Rows:
    """DB 흉내. 이 테스트가 쓰는 만큼만."""

    def __init__(self, store: dict) -> None:
        self.store = store

    def scalars(self):
        return self

    def all(self):
        return list(self.store.values())


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def db(monkeypatch):
    """행 저장소를 메모리로 바꾼다. 실제 DB 없이 상태 전이만 본다."""
    store: dict[int, _Row] = {}
    seq = {"n": 0}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row):
            seq["n"] += 1
            row.id = seq["n"]
            row.finished_at = None
            store[row.id] = row

        async def execute(self, stmt):
            # update(...).values(...) 만 온다. 어떤 행을 어떻게 바꿀지는
            # 문장을 뜯는 대신 테스트가 직접 해석한다.
            values = dict(stmt._values or {})
            resolved = {k.name: getattr(v, "value", v) for k, v in values.items()}
            targets = [r for r in store.values() if _matches(stmt, r)]
            for row in targets:
                for k, v in resolved.items():
                    setattr(row, k, v)
            return _Result(len(targets), store)

        async def commit(self):
            pass

    def _matches(stmt, row) -> bool:
        # `컬럼 == 값` 한 조각만 온다. 실제로 그 컬럼을 비교해야 한다 —
        # 대충 True 를 돌려주면 '한 건만 갱신' 이 깨져도 테스트가 통과해 버린다.
        clause = stmt.whereclause
        return getattr(row, clause.left.name) == clause.right.value

    class _Result:
        def __init__(self, rowcount, store):
            self.rowcount = rowcount
            self._store = store

        def scalars(self):
            return _Rows(self._store)

    monkeypatch.setattr(background, "async_session", _Session)
    return store


@pytest.fixture
def quiet(monkeypatch):
    """알림을 가로챈다."""
    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)

    monkeypatch.setattr(background.manager, "broadcast", fake_broadcast)
    return sent


# --- 시작 ---


async def test_start_returns_immediately(db, quiet, monkeypatch):
    """여기서 기다리면 맡기는 의미가 없다."""
    started = asyncio.Event()

    async def slow(prompt, *, thread_prefix, timeout):
        started.set()
        await asyncio.sleep(3)
        return Outcome("늦게 끝남", "", ok=True)

    monkeypatch.setattr(background, "run_prompt", slow)

    task_id = await asyncio.wait_for(background.start("오래 걸리는 조사"), timeout=0.5)

    assert task_id == 1
    assert db[task_id].status == "running"
    await asyncio.wait_for(started.wait(), timeout=1)  # 실제로 돌기는 한다


# --- 끝 ---


async def test_success_is_recorded_and_announced(db, quiet, monkeypatch):
    async def ok(prompt, *, thread_prefix, timeout):
        return Outcome("장비 세 가지를 정리했습니다.", "", ok=True)

    monkeypatch.setattr(background, "run_prompt", ok)

    task_id = await background.start("카이트 장비 조사")
    await _drain()

    assert db[task_id].status == "done"
    assert "장비 세 가지" in db[task_id].result
    assert db[task_id].finished_at is not None
    assert len(quiet) == 1
    assert "카이트 장비 조사" in quiet[0]["content"]
    assert quiet[0]["type"] == "proactive"


async def test_finishing_one_task_leaves_the_others_alone(db, quiet, monkeypatch):
    """여러 건을 동시에 맡길 수 있다. 하나가 끝날 때 나머지를 덮으면 안 된다."""
    db[99] = _Row(id=99, request="다른 일", status="running", result="", finished_at=None)

    async def ok(prompt, *, thread_prefix, timeout):
        return Outcome("끝", "", ok=True)

    monkeypatch.setattr(background, "run_prompt", ok)

    task_id = await background.start("내 일")
    await _drain()

    assert db[task_id].status == "done"
    assert db[99].status == "running"  # 남의 일은 그대로


async def test_failure_is_recorded_and_still_announced(db, quiet, monkeypatch):
    """실패를 조용히 삼키면 영원히 기다리게 된다."""

    async def bad(prompt, *, thread_prefix, timeout):
        return Outcome("작업이 제한 시간 안에 끝나지 않았습니다.", "", ok=False)

    monkeypatch.setattr(background, "run_prompt", bad)

    task_id = await background.start("끝나지 않는 일")
    await _drain()

    assert db[task_id].status == "failed"
    assert len(quiet) == 1  # 실패해도 알린다


# --- 재시작 ---


async def test_stale_running_rows_are_cleared(db, monkeypatch):
    """서버가 죽으면 진행 중이던 작업은 살릴 수 없다."""
    db[1] = _Row(id=1, request="a", status="running", result="", finished_at=None)
    db[2] = _Row(id=2, request="b", status="done", result="끝", finished_at="어제")

    cleared = await background.clear_stale()

    assert cleared == 1
    assert db[1].status == "interrupted"
    assert "다시 시작" in db[1].result
    assert db[2].status == "done"  # 끝난 건 건드리지 않는다


async def _drain() -> None:
    """띄워 둔 워커가 끝날 때까지 기다린다."""
    for _ in range(50):
        if not background._running:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("백그라운드 작업이 끝나지 않았습니다")
