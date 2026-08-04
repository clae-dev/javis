"""세컨브레인 — 내가 쓴 노트를 읽고, 찾고, 덧붙이고, 귀로 듣는다.

장기 기억이 '대화에서 추려낸 문장'이라면 이쪽은 '내가 직접 쓴 글'이다. 색인은
app/memory/indexer.py 가 백그라운드로 돌리고, 여기서는 그 결과를 쓰기만 한다.

경로는 전부 NOTES_PATH 안으로 묶는다. 말 한마디로 임의 파일이 읽히거나 덮이면 안 된다.
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.config import settings
from app.llm import fast
from app.memory import indexer
from app.voice import tts

# TTS 한 번에 넣을 수 있는 글자 수에 한도가 있다. 대본을 그 아래로 잡아 한 번에 합성한다.
MAX_SCRIPT = 3500
# 한국어 음성은 대략 분당 350자 안팎이다. 정확할 필요는 없고 대본 길이만 잡으면 된다.
CHARS_PER_MINUTE = 350

_SAFE_NAME = re.compile(r"[^0-9A-Za-z가-힣]+")

NARRATE_PROMPT = """너는 사용자의 노트를 소리 내어 읽어 주는 사람이다.

주어진 노트 조각들을 엮어 '귀로 듣는 대본'을 쓴다.
- 눈으로 읽는 글이 아니라 듣는 글이다. 목록·표·마크다운 기호·괄호 주석·URL 을 쓰지 않는다.
- 조각을 그대로 옮기지 말고, 하나의 흐름으로 이어서 설명한다.
- 노트에 없는 내용은 지어내지 않는다. 근거가 부족하면 그렇다고 말한다.
- 도입 한 문장으로 무엇에 대한 이야기인지 알려주고, 마지막에 한 문장으로 정리한다.
- 대본 본문만 출력한다. 제목이나 머리말을 붙이지 않는다."""


def _root() -> Path | None:
    if not settings.notes_path:
        return None
    root = Path(settings.notes_path).expanduser()
    return root if root.is_dir() else None


def _resolve(raw: str) -> tuple[Path | None, str | None]:
    """노트 경로를 풀되 노트 폴더 밖으로는 못 나가게 막는다.

    `..` 이나 절대경로, 심볼릭 링크로 폴더를 빠져나가는 걸 전부 여기서 잘라낸다.
    """
    root = _root()
    if root is None:
        return None, indexer.SETUP_HINT

    name = (raw or "").strip()
    if not name:
        return None, "어느 노트인지 알려주세요."

    # 절대경로는 아예 받지 않는다. 조용히 상대경로로 바꿔 버리면 모델이 뭘 잘못 넣었는지
    # 모른 채 "노트가 없다"는 답만 받는다. 드라이브 문자(C:)도 여기서 걸린다.
    if name.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", name):
        return None, "노트 폴더 기준 상대 경로로 알려주세요 (예: 회고/7월.md)."

    candidate = root / name
    try:
        resolved = candidate.resolve()
        base = root.resolve()
    except OSError as exc:
        return None, f"경로를 확인하지 못했습니다: {exc}"

    if resolved != base and base not in resolved.parents:
        return None, "노트 폴더 밖의 경로는 다룰 수 없습니다."
    return resolved, None


# --- 도구 ---


@tool
async def search_notes(query: str, top_k: int = 5) -> list[dict] | str:
    """내 노트에서 관련 내용을 찾는다. 옵시디언 vault 등 색인해 둔 문서를 뒤진다.

    "예전에 정리해 둔 것", "내가 적어 놓은" 같은 요청이나, 개인적인 기록을 봐야
    답할 수 있는 질문에 쓴다. 대화에서 기억한 것을 찾을 때는 recall 을 쓴다.

    Args:
        query: 찾고 싶은 주제나 질문.
        top_k: 가져올 조각 수 (기본 5, 최대 20).
    """
    if _root() is None:
        return indexer.SETUP_HINT
    hits = await indexer.search(query, top_k=max(1, min(top_k, 20)))
    return hits or "노트에서 관련된 내용을 찾지 못했습니다."


@tool
async def read_note(path: str) -> str:
    """노트 파일 하나를 통째로 읽는다. 검색으로 찾은 노트를 자세히 볼 때 쓴다.

    Args:
        path: 노트 폴더 기준 경로 (예: "회고/2026-07.md"). search_notes 가 돌려준 note 값을 그대로 쓴다.
    """
    resolved, err = _resolve(path)
    if err:
        return err
    if not resolved.is_file():
        return f"'{path}' 노트를 찾을 수 없습니다."

    try:
        text = await asyncio.to_thread(indexer.read_text, resolved)
    except OSError as exc:
        return f"노트를 읽지 못했습니다: {exc}"

    if not text.strip():
        return f"'{path}' 는 비어 있습니다."
    if len(text) > 8000:
        text = text[:8000] + "\n\n…(이하 생략)"
    return text


@tool
async def append_note(path: str, content: str) -> str:
    """노트에 내용을 덧붙인다. 없으면 새로 만든다. 실행 전 사용자 확인을 거친다.

    기존 내용을 지우지 않고 맨 끝에만 추가한다.

    Args:
        path: 노트 폴더 기준 경로 (예: "메모/생각.md"). 확장자가 없으면 .md 를 붙인다.
        content: 덧붙일 내용.
    """
    if not content.strip():
        return "덧붙일 내용이 비어 있습니다."

    resolved, err = _resolve(path)
    if err:
        return err
    if not resolved.suffix:
        resolved = resolved.with_suffix(".md")

    existed = resolved.is_file()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = f"\n\n## {stamp}\n\n{content.strip()}\n" if existed else f"# {resolved.stem}\n\n{content.strip()}\n"

    def write() -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("a", encoding="utf-8") as f:
            f.write(block)

    try:
        await asyncio.to_thread(write)
    except OSError as exc:
        return f"노트에 쓰지 못했습니다: {exc}"

    # 다음 주기를 기다리지 않고 바로 검색되게 한다.
    await indexer.index_one(resolved)

    root = _root()
    shown = resolved.relative_to(root.resolve()).as_posix() if root else resolved.name
    return f"{'덧붙였습니다' if existed else '새 노트를 만들었습니다'}: {shown}"


@tool
async def reindex_notes() -> str:
    """노트 폴더를 다시 훑어 색인을 갱신한다.

    색인은 주기적으로 알아서 돌아간다. 방금 밖에서 노트를 고쳤는데 검색에 안 잡힐 때만 쓴다.
    """
    return await indexer.reindex()


@tool
async def narrate_notes(topic: str, minutes: int = 4) -> str:
    """노트를 엮어 귀로 들을 수 있는 음성 파일로 만든다. 이동 중이나 운동할 때 듣는 용도.

    Args:
        topic: 무엇에 대해 들을지 (예: "지난달 회고", "카이트서핑 장비 정리").
        minutes: 목표 길이(분). 기본 4분, 최대 10분.
    """
    if _root() is None:
        return indexer.SETUP_HINT
    if not settings.has_openai:
        return "OPENAI_API_KEY 가 없어 음성을 만들 수 없습니다."

    hits = await indexer.search(topic, top_k=12)
    if not hits:
        return f"'{topic}' 에 대해 노트에서 찾은 내용이 없습니다."

    target = min(max(int(minutes), 1), 10) * CHARS_PER_MINUTE
    target = min(target, MAX_SCRIPT)

    source = "\n\n".join(f"[{h['note']} — {h['section']}]\n{h['content']}" for h in hits)
    message = f"주제: {topic}\n목표 길이: 약 {target}자\n\n[노트 조각]\n{source}"

    reply = await fast(temperature=0.4).ainvoke(
        [SystemMessage(content=NARRATE_PROMPT), HumanMessage(content=message)]
    )
    script = (reply.content if isinstance(reply.content, str) else str(reply.content)).strip()
    if not script:
        return "대본을 만들지 못했습니다."
    script = script[:MAX_SCRIPT]

    audio = bytearray()
    try:
        async for chunk in tts.synthesize(script, fmt="mp3"):
            audio.extend(chunk)
    except Exception as exc:
        return f"음성 합성에 실패했습니다: {exc}"

    slug = _SAFE_NAME.sub("-", topic).strip("-")[:40] or "notes"
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.mp3"
    directory = Path(settings.podcasts_path)

    def write() -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(bytes(audio))

    await asyncio.to_thread(write)

    notes = ", ".join(sorted({h["note"] for h in hits})[:3])
    return (
        f"'{topic}' 을(를) 약 {len(script) // CHARS_PER_MINUTE + 1}분 분량으로 만들었습니다. "
        f"주소: /podcast/{name} (참고한 노트: {notes})"
    )
