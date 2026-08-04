"""노트 폴더 색인.

옵시디언 vault 든 그냥 문서 폴더든, NOTES_PATH 아래를 훑어 헤딩 단위로 쪼갠 뒤
pgvector 에 넣는다. 장기 기억(long_term)이 '대화에서 추려낸 문장'을 다룬다면
이쪽은 '내가 직접 쓴 글'을 다룬다.

증분으로 돈다. mtime 이 그대로면 파일을 열지도 않고, mtime 만 바뀌고 내용이 같으면
(편집기가 저장만 다시 한 경우) 임베딩을 다시 만들지 않는다. 노트 폴더가 커질수록
이 두 단계가 비용의 대부분을 걷어낸다.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.config import settings
from app.db.models import Document, DocumentChunk
from app.db.session import async_session
from app.llm import embeddings

log = logging.getLogger("javis.notes")

SETUP_HINT = (
    "노트 폴더가 설정되지 않았습니다. .env 의 NOTES_PATH 에 옵시디언 vault 나 "
    "문서 폴더 경로를 넣어 주세요."
)

# 임베딩 한 번에 보낼 조각 수. 너무 크면 한 요청이 실패했을 때 잃는 게 많다.
_EMBED_BATCH = 64

# 옵시디언 내부 폴더와 흔한 잡동사니. 색인해 봐야 검색만 더러워진다.
_SKIP_DIRS = {".obsidian", ".git", ".trash", "node_modules", "__pycache__", ".venv", "venv"}

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_CODE_FENCE = re.compile(r"^\s*(```|~~~)")

# 색인이 두 번 겹쳐 돌면 같은 파일을 두 번 임베딩한다. 스케줄러와 수동 호출 양쪽을 막는다.
_lock = asyncio.Lock()


@dataclass(frozen=True)
class Chunk:
    heading: str
    content: str


# --- 파일 훑기 ---


def _root() -> Path | None:
    if not settings.notes_path:
        return None
    root = Path(settings.notes_path).expanduser()
    return root if root.is_dir() else None


def _extensions() -> set[str]:
    return {
        e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
        for e in settings.notes_extensions.split(",")
        if e.strip()
    }


def _walk(root: Path) -> list[Path]:
    wanted = _extensions()
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in wanted:
            continue
        parts = set(path.relative_to(root).parts[:-1])
        if parts & _SKIP_DIRS or path.name.startswith("."):
            continue
        found.append(path)
    return sorted(found)


# --- 읽기 ---


def read_text(path: Path) -> str:
    """파일 하나를 텍스트로 읽는다. PDF 는 페이지별로 풀어낸다."""
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
    # 노트는 대개 UTF-8 이지만, 윈도우에서 만든 옛 파일이 섞여 있어도 죽지 않게 한다.
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf 가 없어 PDF 를 건너뜁니다: %s", path.name)
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        log.warning("PDF 를 열지 못했습니다(%s): %s", path.name, exc)
        return ""

    pages = []
    for number, page in enumerate(reader.pages, 1):
        try:
            body = (page.extract_text() or "").strip()
        except Exception:
            continue
        if body:
            # 페이지를 헤딩처럼 취급해, 검색 결과에서 몇 쪽인지 알 수 있게 한다.
            pages.append(f"## {number}쪽\n\n{body}")
    return "\n\n".join(pages)


# --- 쪼개기 ---


def _split_long(text: str, limit: int) -> list[str]:
    """긴 문단 덩어리를 문단 경계에서 limit 이하로 자른다."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    out: list[str] = []
    buf = ""
    for block in blocks:
        # 문단 하나가 이미 한도를 넘으면 그것만 따로 잘라 넣는다.
        if len(block) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(block[i : i + limit] for i in range(0, len(block), limit))
            continue
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) > limit:
            out.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def chunk_markdown(text: str, limit: int | None = None) -> list[Chunk]:
    """헤딩 단위로 쪼갠다. 조각마다 상위 헤딩 경로를 붙여 맥락을 남긴다.

    코드 펜스 안의 `#` 은 헤딩이 아니다 — 파이썬 주석을 문단 제목으로 오해하면
    쪼개기가 엉망이 된다.
    """
    limit = limit or settings.notes_chunk_chars
    text = _FRONTMATTER.sub("", text)

    stack: list[str] = []
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False

    for line in text.splitlines():
        if _CODE_FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and (m := _HEADING.match(line)):
            level, title = len(m.group(1)), m.group(2).strip()
            stack = stack[: level - 1]
            stack.append(title)
            sections.append((" > ".join(s for s in stack if s), []))
            continue
        sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        for piece in _split_long(body, limit):
            chunks.append(Chunk(heading=heading, content=piece))
    return chunks


def _embed_text(title: str, chunk: Chunk) -> str:
    """임베딩에 넣을 문자열. 제목·헤딩을 앞에 붙여야 짧은 조각도 맥락을 갖는다."""
    prefix = " > ".join(p for p in (title, chunk.heading) if p)
    return f"{prefix}\n{chunk.content}" if prefix else chunk.content


# --- 색인 ---


async def _index_file(path: Path, root: Path) -> int:
    """파일 하나를 색인한다. 새로 만든 조각 수를 돌려준다(변경 없으면 0)."""
    rel = path.relative_to(root).as_posix()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        log.warning("파일 정보를 읽지 못했습니다(%s): %s", rel, exc)
        return 0

    async with async_session() as session:
        row = (
            await session.execute(select(Document).where(Document.path == rel))
        ).scalar_one_or_none()
        # 가장 흔한 경우: 아무것도 안 바뀌었다. 파일을 열지도 않는다.
        if row is not None and row.mtime == mtime:
            return 0

    text = await asyncio.to_thread(read_text, path)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    async with async_session() as session:
        row = (
            await session.execute(select(Document).where(Document.path == rel))
        ).scalar_one_or_none()

        # 저장만 다시 눌러 mtime 이 밀린 경우. 내용이 같으니 임베딩은 그대로 둔다.
        if row is not None and row.sha == sha:
            row.mtime = mtime
            await session.commit()
            return 0

        chunks = chunk_markdown(text)
        title = path.stem

        if not chunks:
            # 빈 파일도 기록은 남긴다. 안 그러면 매 주기마다 다시 읽는다.
            if row is None:
                session.add(Document(path=rel, title=title, sha=sha, mtime=mtime, chunks=0))
            else:
                row.title, row.sha, row.mtime, row.chunks = title, sha, mtime, 0
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == row.id)
                )
            await session.commit()
            return 0

    vectors: list[list[float]] = []
    payloads = [_embed_text(path.stem, c) for c in chunks]
    for i in range(0, len(payloads), _EMBED_BATCH):
        vectors.extend(await embeddings().aembed_documents(payloads[i : i + _EMBED_BATCH]))

    async with async_session() as session:
        row = (
            await session.execute(select(Document).where(Document.path == rel))
        ).scalar_one_or_none()
        if row is None:
            row = Document(path=rel, title=path.stem, sha=sha, mtime=mtime, chunks=len(chunks))
            session.add(row)
            await session.flush()
        else:
            row.title, row.sha, row.mtime, row.chunks = path.stem, sha, mtime, len(chunks)
            # 조각은 통째로 갈아 끼운다. 부분 갱신은 순서가 꼬이기 쉽고 이득이 적다.
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == row.id)
            )

        session.add_all(
            [
                DocumentChunk(
                    document_id=row.id,
                    ordinal=i,
                    heading=chunk.heading[:512],
                    content=chunk.content,
                    embedding=vec,
                )
                for i, (chunk, vec) in enumerate(zip(chunks, vectors))
            ]
        )
        await session.commit()

    return len(chunks)


async def _drop_missing(seen: set[str]) -> int:
    """폴더에서 사라진 노트를 색인에서도 지운다."""
    async with async_session() as session:
        rows = (await session.execute(select(Document))).scalars().all()
        gone = [d for d in rows if d.path not in seen]
        for doc in gone:
            await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
            await session.delete(doc)
        if gone:
            await session.commit()
    return len(gone)


async def reindex() -> str:
    """노트 폴더를 증분 색인한다. 사람이 읽을 결과 문장을 돌려준다."""
    root = _root()
    if root is None:
        return SETUP_HINT

    if _lock.locked():
        return "이미 색인 중입니다. 잠시 뒤에 다시 확인해 주세요."

    async with _lock:
        files = await asyncio.to_thread(_walk, root)
        indexed = 0
        chunks = 0
        failed = 0
        for path in files:
            try:
                made = await _index_file(path, root)
            except Exception as exc:
                # 파일 하나가 깨져도 나머지는 색인한다. 다만 조용히 넘기지는 않는다 —
                # 전부 실패해도 "바뀐 내용 없음"으로 보이면 고장을 눈치챌 수가 없다.
                log.warning("노트 색인 실패(%s): %s", path.name, exc)
                failed += 1
                continue
            if made:
                indexed += 1
                chunks += made

        removed = await _drop_missing({p.relative_to(root).as_posix() for p in files})

    parts = [f"노트 {len(files)}개 확인"]
    if indexed:
        parts.append(f"{indexed}개 새로 색인({chunks}조각)")
    if removed:
        parts.append(f"{removed}개 삭제 반영")
    if failed:
        parts.append(f"{failed}개 실패(로그 확인)")
    if not indexed and not removed and not failed:
        parts.append("바뀐 내용 없음")
    return ", ".join(parts)


async def index_one(path: Path) -> None:
    """파일 하나만 즉시 색인한다. 노트를 방금 쓴 직후에 쓴다."""
    root = _root()
    if root is None:
        return
    try:
        await _index_file(path, root)
    except Exception as exc:
        log.warning("노트 색인 실패(%s): %s", path.name, exc)


# --- 검색 ---


async def search(query: str, top_k: int = 5) -> list[dict]:
    query = query.strip()
    if not query:
        return []

    vec = await embeddings().aembed_query(query)
    async with async_session() as session:
        stmt = (
            select(
                Document.path,
                Document.title,
                DocumentChunk.heading,
                DocumentChunk.content,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .order_by(DocumentChunk.embedding.l2_distance(vec))
            .limit(top_k)
        )
        rows = (await session.execute(stmt)).all()

    return [
        {
            "note": path,
            "section": heading or title,
            "content": content,
        }
        for path, title, heading, content in rows
    ]
