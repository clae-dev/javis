"""색인 한 바퀴가 DB 를 몇 번 여는지.

노트 색인은 10분마다 도는데, 거의 매번 "바뀐 게 없다"로 끝난다. 그 흔한 경우에
파일마다 조회가 나가면 vault 가 커질수록 아무 일도 안 하면서 왕복만 쌓인다.
눈으로는 안 보이는 종류의 퇴행이라 세션 수를 직접 센다.
"""

import hashlib

import pytest

from app.memory import indexer


# --- DB 흉내 ---


class _Rows:
    """execute() 결과. 색인이 실제로 쓰는 만큼만 흉내 낸다."""

    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return None  # 이 테스트에서 문서 행은 항상 새로 만든다


class _FakeSession:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _Rows(self._rows)

    async def flush(self):
        pass

    async def commit(self):
        pass

    def add(self, _obj):
        pass

    def add_all(self, _objs):
        pass


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.calls = 0

    async def aembed_documents(self, payloads):
        self.calls += 1
        return [[0.0, 0.0, 0.0, 0.0] for _ in payloads]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    for i in range(8):
        (tmp_path / f"노트{i}.md").write_text(f"# 제목{i}\n\n내용{i}\n", encoding="utf-8")
    monkeypatch.setattr(indexer.settings, "notes_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def wired(vault, monkeypatch):
    """색인된 상태가 파일과 정확히 일치하는 DB 를 물린다. (열린 세션 수, 임베더)"""
    rows = [
        (
            path.relative_to(vault).as_posix(),
            path.stat().st_mtime,
            hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
        )
        for path in indexer._walk(vault)
    ]
    opened: list[int] = []

    def factory():
        opened.append(1)
        return _FakeSession(rows)

    embedder = _FakeEmbeddings()
    monkeypatch.setattr(indexer, "async_session", factory)
    monkeypatch.setattr(indexer, "embeddings", lambda: embedder)
    return opened, embedder


# --- 바뀐 게 없을 때 ---


async def test_unchanged_vault_opens_one_session(wired):
    """파일이 몇 개든 상태를 한 번 받아 오는 게 전부여야 한다."""
    opened, _ = wired

    assert "바뀐 내용 없음" in await indexer.reindex()
    assert len(opened) == 1


async def test_unchanged_vault_never_embeds(wired):
    _, embedder = wired

    await indexer.reindex()
    assert embedder.calls == 0


# --- 바뀌었을 때 ---


async def test_changed_file_is_reindexed(vault, wired):
    """건너뛰기가 너무 공격적이면 고친 노트가 조용히 묻힌다."""
    opened, embedder = wired
    (vault / "노트3.md").write_text("# 제목3\n\n내용을 고쳤다\n", encoding="utf-8")

    result = await indexer.reindex()

    assert "1개 새로 색인" in result
    assert embedder.calls == 1
    assert len(opened) == 2  # 상태 한 번 + 고친 파일 한 번


async def test_new_file_is_picked_up(vault, wired):
    _, embedder = wired
    (vault / "새노트.md").write_text("# 새 글\n\n방금 썼다\n", encoding="utf-8")

    assert "1개 새로 색인" in await indexer.reindex()
    assert embedder.calls == 1
