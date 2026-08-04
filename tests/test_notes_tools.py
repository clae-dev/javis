"""노트 도구.

제일 중요한 건 경로 가두기다. 이 도구들은 음성 한마디로 불리고, 인자는 모델이 만든다.
노트 폴더 밖으로 새는 순간 임의 파일을 읽거나 덮어쓸 수 있다.
"""

import pytest

from app.tools import notes


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "회고").mkdir(parents=True)
    (root / "회고" / "7월.md").write_text("# 7월\n\n카이트서핑을 갔다.\n", encoding="utf-8")

    # 폴더 밖에 두는 미끼 — 여기에 닿으면 안 된다.
    (tmp_path / "비밀.md").write_text("건드리면 안 되는 파일", encoding="utf-8")

    monkeypatch.setattr(notes.settings, "notes_path", str(root))
    monkeypatch.setattr(notes.indexer.settings, "notes_path", str(root))

    async def noop(_path):
        return None

    monkeypatch.setattr(notes.indexer, "index_one", noop)
    return root


# --- 경로 가두기 ---


@pytest.mark.parametrize(
    "path",
    [
        "../비밀.md",
        "회고/../../비밀.md",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "..\\비밀.md",
        "회고/../../../../../../etc/hosts",
    ],
)
def test_paths_outside_the_vault_are_refused(vault, path):
    resolved, err = notes._resolve(path)
    assert resolved is None
    assert err is not None


@pytest.mark.parametrize("path", ["회고/7월.md", "새폴더/새노트.md", "루트.md"])
def test_paths_inside_the_vault_pass(vault, path):
    resolved, err = notes._resolve(path)
    assert err is None
    assert vault.resolve() in resolved.parents or resolved.parent == vault.resolve()


def test_empty_path_is_refused(vault):
    assert notes._resolve("   ")[0] is None


def test_resolve_without_vault_returns_hint(monkeypatch):
    monkeypatch.setattr(notes.settings, "notes_path", "")
    resolved, err = notes._resolve("아무거나.md")
    assert resolved is None and "NOTES_PATH" in err


# --- 읽기 ---


async def test_read_note_returns_content(vault):
    assert "카이트서핑" in await notes.read_note.ainvoke({"path": "회고/7월.md"})


async def test_read_note_refuses_escape(vault):
    result = await notes.read_note.ainvoke({"path": "../비밀.md"})
    assert "건드리면 안 되는" not in result
    assert "밖의 경로" in result


async def test_read_missing_note_is_reported(vault):
    assert "찾을 수 없" in await notes.read_note.ainvoke({"path": "없는노트.md"})


async def test_read_note_truncates_huge_files(vault):
    (vault / "긴글.md").write_text("가" * 20000, encoding="utf-8")
    result = await notes.read_note.ainvoke({"path": "긴글.md"})
    assert "생략" in result and len(result) < 20000


# --- 쓰기 ---


async def test_append_creates_a_new_note(vault):
    result = await notes.append_note.ainvoke({"path": "메모/새것.md", "content": "첫 줄"})
    written = (vault / "메모" / "새것.md").read_text(encoding="utf-8")

    assert "새 노트를 만들었습니다" in result
    assert "첫 줄" in written
    assert written.startswith("# 새것")


async def test_append_keeps_existing_content(vault):
    await notes.append_note.ainvoke({"path": "회고/7월.md", "content": "덧붙인 줄"})
    written = (vault / "회고" / "7월.md").read_text(encoding="utf-8")

    assert "카이트서핑을 갔다." in written  # 기존 내용이 살아 있다
    assert "덧붙인 줄" in written


async def test_append_adds_md_suffix(vault):
    await notes.append_note.ainvoke({"path": "메모/확장자없음", "content": "내용"})
    assert (vault / "메모" / "확장자없음.md").is_file()


async def test_append_refuses_escape(vault):
    outside = vault.parent / "비밀.md"
    before = outside.read_text(encoding="utf-8")

    result = await notes.append_note.ainvoke({"path": "../비밀.md", "content": "침입"})

    assert "밖의 경로" in result
    assert outside.read_text(encoding="utf-8") == before  # 손대지 않았다


async def test_append_refuses_empty_content(vault):
    assert "비어" in await notes.append_note.ainvoke({"path": "메모/x.md", "content": "  "})


# --- 검색 ---


async def test_search_without_vault_returns_hint(monkeypatch):
    monkeypatch.setattr(notes.settings, "notes_path", "")
    assert await notes.search_notes.ainvoke({"query": "회고"}) == notes.indexer.SETUP_HINT


async def test_search_reports_empty_result(vault, monkeypatch):
    async def nothing(query, top_k):
        return []

    monkeypatch.setattr(notes.indexer, "search", nothing)
    assert "찾지 못했" in await notes.search_notes.ainvoke({"query": "없는주제"})


async def test_search_clamps_top_k(vault, monkeypatch):
    seen = {}

    async def capture(query, top_k):
        seen["top_k"] = top_k
        return [{"note": "a", "section": "b", "content": "c"}]

    monkeypatch.setattr(notes.indexer, "search", capture)
    await notes.search_notes.ainvoke({"query": "회고", "top_k": 999})
    assert seen["top_k"] == 20


# --- 음성으로 듣기 ---


async def test_narrate_needs_notes_to_work_from(vault, monkeypatch):
    async def nothing(query, top_k):
        return []

    monkeypatch.setattr(notes.indexer, "search", nothing)
    result = await notes.narrate_notes.ainvoke({"topic": "없는주제"})
    assert "찾은 내용이 없습니다" in result


async def test_narrate_writes_an_mp3(vault, tmp_path, monkeypatch):
    monkeypatch.setattr(notes.settings, "podcasts_path", str(tmp_path / "podcasts"))
    monkeypatch.setattr(notes.settings, "openai_api_key", "test-key")

    async def hits(query, top_k):
        return [{"note": "회고/7월.md", "section": "7월", "content": "카이트서핑을 갔다."}]

    class _Reply:
        content = "7월에는 카이트서핑을 자주 갔습니다."

    class _LLM:
        async def ainvoke(self, _messages):
            return _Reply()

    async def fake_tts(text, fmt="mp3"):
        yield b"ID3fake-audio"

    monkeypatch.setattr(notes.indexer, "search", hits)
    monkeypatch.setattr(notes, "fast", lambda **_: _LLM())
    monkeypatch.setattr(notes.tts, "synthesize", fake_tts)

    result = await notes.narrate_notes.ainvoke({"topic": "7월 회고"})

    written = list((tmp_path / "podcasts").glob("*.mp3"))
    assert len(written) == 1
    assert written[0].read_bytes() == b"ID3fake-audio"
    assert "/podcast/" in result


async def test_narrate_filename_is_safe(vault, tmp_path, monkeypatch):
    """주제는 모델이 만든 문자열이다. 그대로 파일명에 쓰면 안 된다."""
    monkeypatch.setattr(notes.settings, "podcasts_path", str(tmp_path / "podcasts"))
    monkeypatch.setattr(notes.settings, "openai_api_key", "test-key")

    async def hits(query, top_k):
        return [{"note": "a.md", "section": "s", "content": "c"}]

    class _LLM:
        async def ainvoke(self, _messages):
            return type("R", (), {"content": "대본"})()

    async def fake_tts(text, fmt="mp3"):
        yield b"x"

    monkeypatch.setattr(notes.indexer, "search", hits)
    monkeypatch.setattr(notes, "fast", lambda **_: _LLM())
    monkeypatch.setattr(notes.tts, "synthesize", fake_tts)

    await notes.narrate_notes.ainvoke({"topic": "../../탈출 /etc/passwd"})

    written = list((tmp_path / "podcasts").glob("*.mp3"))
    assert len(written) == 1
    assert "/" not in written[0].name and ".." not in written[0].name
