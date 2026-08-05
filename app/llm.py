"""OpenAI 클라이언트 팩토리.

모델 인스턴스를 캐싱하고, 재시도/타임아웃 같은 견고성 기본값을 한곳에서 박아둔다.
키가 없어도 import 는 통과하고, 실제 호출 시점에 에러가 난다.
"""

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from openai import AsyncOpenAI

from app.config import settings

_chat_cache: dict[str, ChatOpenAI] = {}
_embeddings: OpenAIEmbeddings | None = None
_openai: AsyncOpenAI | None = None


def chat(model: str | None = None, *, streaming: bool = False, temperature: float = 0.3) -> ChatOpenAI:
    model = model or settings.llm_model
    key = f"{model}:{streaming}:{temperature}"
    if key not in _chat_cache:
        _chat_cache[key] = ChatOpenAI(
            model=model,
            temperature=temperature,
            streaming=streaming,
            max_retries=3,
            timeout=60,
        )
    return _chat_cache[key]


def fast(temperature: float = 0.0) -> ChatOpenAI:
    return chat(settings.fast_model, temperature=temperature)


def embeddings() -> OpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        kwargs = {"model": settings.embedding_model, "max_retries": 3}
        # text-embedding-3 계열만 차원 축소를 지원한다. EMBEDDING_DIM 을 낮추면 벡터가
        # 작아져 HNSW 검색·적재가 가벼워진다. 옛 모델(ada-002 등)에 넘기면 에러라 거른다.
        if settings.embedding_model.startswith("text-embedding-3"):
            kwargs["dimensions"] = settings.embedding_dim
        _embeddings = OpenAIEmbeddings(**kwargs)
    return _embeddings


def openai_client() -> AsyncOpenAI:
    """음성(STT/TTS)처럼 langchain 래퍼가 없는 기능에 쓰는 원시 클라이언트."""
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(max_retries=3)
    return _openai
