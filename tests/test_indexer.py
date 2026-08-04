"""노트 쪼개기와 파일 훑기.

조각을 어떻게 나누느냐가 검색 품질을 그대로 결정한다. DB 없이 검증할 수 있는
순수 함수만 본다 — 색인·임베딩은 통합 테스트 영역이다.
"""

import pytest

from app.memory import indexer
from app.memory.indexer import chunk_markdown

NOTE = """---
tags: [회고, 2026]
---

머리말 문단.

# 7월 회고

이번 달은 카이트서핑을 자주 갔다.

## 잘한 것

아침 루틴을 지켰다.

## 아쉬운 것

책을 못 읽었다.

# 8월 계획

제주에 간다.
"""


# --- 헤딩 쪼개기 ---


def test_frontmatter_is_stripped():
    text = "\n".join(c.content for c in chunk_markdown(NOTE))
    assert "tags:" not in text
    assert "머리말 문단." in text


def test_sections_split_on_headings():
    headings = [c.heading for c in chunk_markdown(NOTE)]
    assert headings == ["", "7월 회고", "7월 회고 > 잘한 것", "7월 회고 > 아쉬운 것", "8월 계획"]


def test_heading_path_is_a_breadcrumb():
    """조각 하나만 건져 올려도 어느 문단인지 알아야 한다."""
    by_heading = {c.heading: c.content for c in chunk_markdown(NOTE)}
    assert by_heading["7월 회고 > 잘한 것"].strip() == "아침 루틴을 지켰다."


def test_sibling_heading_pops_the_stack():
    """같은 레벨 헤딩이 나오면 이전 형제를 물려받으면 안 된다."""
    headings = [c.heading for c in chunk_markdown(NOTE)]
    assert "8월 계획" in headings
    assert not any(h.startswith("7월 회고") and "8월" in h for h in headings)


def test_empty_sections_are_dropped():
    chunks = chunk_markdown("# 제목만 있고\n\n## 내용은 없다\n")
    assert chunks == []


# --- 코드 펜스 ---


def test_hash_inside_code_fence_is_not_a_heading():
    """파이썬 주석을 문단 제목으로 오해하면 쪼개기가 통째로 어긋난다."""
    note = """# 스크립트 메모

```python
# 이건 주석이지 헤딩이 아니다
x = 1
```

설명 문단.
"""
    headings = [c.heading for c in chunk_markdown(note)]
    assert headings == ["스크립트 메모"]
    assert "x = 1" in chunk_markdown(note)[0].content


def test_tilde_fence_also_counts():
    note = "# 제목\n\n~~~\n# 주석\n~~~\n"
    assert [c.heading for c in chunk_markdown(note)] == ["제목"]


# --- 길이 자르기 ---


def test_long_section_splits_on_paragraph_boundaries():
    body = "\n\n".join(f"문단 {i} " + "가" * 200 for i in range(10))
    chunks = chunk_markdown(f"# 긴 글\n\n{body}", limit=500)

    assert len(chunks) > 1
    assert all(len(c.content) <= 500 for c in chunks)
    assert all(c.heading == "긴 글" for c in chunks)  # 조각마다 맥락이 남는다


def test_single_paragraph_longer_than_limit_is_hard_split():
    chunks = chunk_markdown("# 제목\n\n" + "가" * 1000, limit=300)
    assert len(chunks) == 4
    assert all(len(c.content) <= 300 for c in chunks)


def test_nothing_is_lost_when_splitting():
    body = "\n\n".join(f"문단{i}" for i in range(50))
    joined = "".join(c.content for c in chunk_markdown(f"# 제목\n\n{body}", limit=100))
    for i in range(50):
        assert f"문단{i}" in joined


# --- 폴더 훑기 ---


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "메모").mkdir()
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)

    (tmp_path / "메모" / "생각.md").write_text("내용", encoding="utf-8")
    (tmp_path / "루트.txt").write_text("내용", encoding="utf-8")
    (tmp_path / "표.csv").write_text("a,b", encoding="utf-8")
    (tmp_path / ".숨김.md").write_text("내용", encoding="utf-8")
    (tmp_path / ".obsidian" / "workspace.md").write_text("설정", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg" / "README.md").write_text("남의 것", encoding="utf-8")

    monkeypatch.setattr(indexer.settings, "notes_path", str(tmp_path))
    return tmp_path


def test_walk_picks_wanted_extensions_only(vault):
    names = {p.name for p in indexer._walk(vault)}
    assert names == {"생각.md", "루트.txt"}


def test_walk_skips_obsidian_and_vendor_dirs(vault):
    paths = {p.as_posix() for p in indexer._walk(vault)}
    assert not any(".obsidian" in p or "node_modules" in p for p in paths)


def test_walk_skips_dotfiles(vault):
    assert all(not p.name.startswith(".") for p in indexer._walk(vault))


def test_extensions_parse_with_or_without_dot(monkeypatch):
    monkeypatch.setattr(indexer.settings, "notes_extensions", "md, .txt ,PDF")
    assert indexer._extensions() == {".md", ".txt", ".pdf"}


def test_root_is_none_when_unset(monkeypatch):
    monkeypatch.setattr(indexer.settings, "notes_path", "")
    assert indexer._root() is None


def test_root_is_none_when_folder_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(indexer.settings, "notes_path", str(tmp_path / "없는폴더"))
    assert indexer._root() is None


async def test_reindex_without_folder_returns_hint(monkeypatch):
    monkeypatch.setattr(indexer.settings, "notes_path", "")
    assert await indexer.reindex() == indexer.SETUP_HINT
