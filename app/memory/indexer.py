"""노트 폴더 색인.

옵시디언 vault 든 그냥 문서 폴더든, NOTES_PATH 아래를 훑어 헤딩 단위로 쪼갠 뒤
pgvector 에 넣는다. 장기 기억(long_term)이 '대화에서 추려낸 문장'을 다룬다면
이쪽은 '내가 직접 쓴 글'을 다룬다.

증분으로 돈다. mtime 이 그대로면 파일을 열지도 않고, mtime 만 바뀌고 내용이 같으면
(편집기가 저장만 다시 한 경우) 임베딩을 다시 만들지 않는다. 노트 폴더가 커질수록
이 두 단계가 비용의 대부분을 걷어낸다.

무엇이 바뀌었는지 판단할 재료(경로별 mtime·sha)는 한 바퀴 시작할 때 통째로 받아 둔다.
파일마다 조회하면 바뀐 게 하나도 없어도 파일 수만큼 DB 왕복이 나간다.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select, update

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

# 동시에 색인할 파일 수. 파일 하나의 시간은 대부분 임베딩 API 를 기다리는 데 쓰이므로
# 몇 개를 겹쳐 돌리면 첫 전체 색인이 그만큼 짧아진다. 너무 올리면 rate limit 에 걸린다.
_INDEX_CONCURRENCY = 4

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


async def _load_state() -> dict[str, tuple[float, str]]:
    """색인해 둔 노트의 {상대경로: (mtime, sha)} 를 한 번에 받는다.

    파일마다 조회하면 바뀐 게 없는 폴더에서도 파일 수만큼 왕복이 나간다. 판단에 쓰는
    건 두 컬럼뿐이라 행 전체(=조각 수천 개의 부모)를 끌어올 이유도 없다.
    """
    async with async_session() as session:
        rows = await session.execute(select(Document.path, Document.mtime, Document.sha))
        return {path: (mtime, sha) for path, mtime, sha in rows}


async def _state_of(rel: str) -> dict[str, tuple[float, str]]:
    """파일 하나짜리 상태. 폴더를 다 훑지 않고 한 건만 색인할 때 쓴다."""
    async with async_session() as session:
        row = (
            await session.execute(
                select(Document.mtime, Document.sha).where(Document.path == rel)
            )
        ).first()
    return {rel: (row[0], row[1])} if row is not None else {}


async def _index_file(
    path: Path, root: Path, known: dict[str, tuple[float, str]] | None = None
) -> int:
    """파일 하나를 색인한다. 새로 만든 조각 수를 돌려준다(변경 없으면 0).

    known 은 미리 받아 둔 {상대경로: (mtime, sha)}. 가장 흔한 경우(아무것도 안 바뀜)를
    DB 를 보지 않고 걸러내려고 호출부에서 통째로 넘겨받는다.
    """
    rel = path.relative_to(root).as_posix()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        log.warning("파일 정보를 읽지 못했습니다(%s): %s", rel, exc)
        return 0

    if known is None:
        known = await _state_of(rel)
    prev = known.get(rel)

    # 가장 흔한 경우: 아무것도 안 바뀌었다. 파일도 DB 도 열지 않는다.
    if prev is not None and prev[0] == mtime:
        return 0

    text = await asyncio.to_thread(read_text, path)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # 저장만 다시 눌러 mtime 이 밀린 경우. 내용이 같으니 임베딩은 그대로 두고 시각만 민다.
    if prev is not None and prev[1] == sha:
        async with async_session() as session:
            await session.execute(
                update(Document).where(Document.path == rel).values(mtime=mtime)
            )
            await session.commit()
        known[rel] = (mtime, sha)
        return 0

    title = path.stem
    chunks = chunk_markdown(text)

    # 임베딩은 세션 밖에서 만든다 — 수십 초짜리 API 왕복 동안 커넥션을 붙들고 있을 이유가 없다.
    vectors: list[list[float]] = []
    if chunks:
        payloads = [_embed_text(title, c) for c in chunks]
        for i in range(0, len(payloads), _EMBED_BATCH):
            vectors.extend(await embeddings().aembed_documents(payloads[i : i + _EMBED_BATCH]))

    async with async_session() as session:
        row = (
            await session.execute(select(Document).where(Document.path == rel))
        ).scalar_one_or_none()
        if row is None:
            # 빈 파일도 기록은 남긴다. 안 그러면 매 주기마다 다시 읽는다.
            row = Document(path=rel, title=title, sha=sha, mtime=mtime, chunks=len(chunks))
            session.add(row)
            await session.flush()
        else:
            row.title, row.sha, row.mtime, row.chunks = title, sha, mtime, len(chunks)
            # 조각은 통째로 갈아 끼운다. 부분 갱신은 순서가 꼬이기 쉽고 이득이 적다.
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == row.id)
            )

        if chunks:
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

    known[rel] = (mtime, sha)
    return len(chunks)


async def _drop_missing(seen: set[str], known: dict[str, tuple[float, str]]) -> int:
    """폴더에서 사라진 노트를 색인에서도 지운다.

    지울 게 있는지는 이미 받아 둔 상태로 판단한다. 대개는 하나도 없어서 쿼리가 안 나간다.
    """
    gone = [rel for rel in known if rel not in seen]
    if not gone:
        return 0

    async with async_session() as session:
        rows = (
            await session.execute(select(Document).where(Document.path.in_(gone)))
        ).scalars().all()
        if rows:
            await session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.document_id.in_([d.id for d in rows])
                )
            )
            for doc in rows:
                await session.delete(doc)
            await session.commit()
    return len(rows)


async def _index_guarded(
    path: Path, root: Path, known: dict[str, tuple[float, str]], sem: asyncio.Semaphore
) -> int | None:
    """세마포어 아래에서 파일 하나를 색인한다. 실패하면 None."""
    async with sem:
        try:
            return await _index_file(path, root, known)
        except Exception as exc:
            # 파일 하나가 깨져도 나머지는 색인한다. 다만 조용히 넘기지는 않는다 —
            # 전부 실패해도 "바뀐 내용 없음"으로 보이면 고장을 눈치챌 수가 없다.
            log.warning("노트 색인 실패(%s): %s", path.name, exc)
            return None


async def reindex() -> str:
    """노트 폴더를 증분 색인한다. 사람이 읽을 결과 문장을 돌려준다."""
    root = _root()
    if root is None:
        return SETUP_HINT

    if _lock.locked():
        return "이미 색인 중입니다. 잠시 뒤에 다시 확인해 주세요."

    async with _lock:
        files = await asyncio.to_thread(_walk, root)
        known = await _load_state()

        sem = asyncio.Semaphore(_INDEX_CONCURRENCY)
        made = await asyncio.gather(*(_index_guarded(p, root, known, sem) for p in files))

        failed = sum(1 for m in made if m is None)
        indexed = sum(1 for m in made if m)
        chunks = sum(m for m in made if m)

        removed = await _drop_missing({p.relative_to(root).as_posix() for p in files}, known)

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
