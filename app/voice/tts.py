from app.config import settings
from app.llm import openai_client


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """텍스트를 mp3 음성 바이트로 합성한다 (OpenAI TTS)."""
    voice = voice or settings.tts_voice
    async with openai_client().audio.speech.with_streaming_response.create(
        model=settings.tts_model,
        voice=voice,
        input=text,
        response_format="mp3",
    ) as response:
        return await response.read()
