import io

from app.config import settings
from app.llm import openai_client


async def transcribe(audio: bytes, filename: str = "audio.webm", language: str = "ko") -> str:
    """음성 바이트를 텍스트로 옮긴다 (Whisper)."""
    buffer = io.BytesIO(audio)
    buffer.name = filename
    result = await openai_client().audio.transcriptions.create(
        model=settings.stt_model,
        file=buffer,
        language=language,
    )
    return result.text
