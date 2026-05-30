from collections.abc import AsyncIterator

from app.config import settings
from app.llm import openai_client


async def synthesize(
    text: str,
    voice: str | None = None,
    instructions: str | None = None,
    fmt: str = "mp3",
) -> AsyncIterator[bytes]:
    """텍스트를 음성 바이트 청크로 흘려보낸다 (OpenAI TTS).

    OpenAI 가 합성한 청크를 받는 대로 yield 하므로, 클라이언트는 전체 합성이 끝나기
    전에 재생을 시작할 수 있다. 전체를 모았다가 한 번에 돌려주던 기존 방식보다
    첫 소리까지의 지연이 짧다.

    gpt-4o 계열 TTS 모델은 instructions 로 말투·감정을 조절할 수 있다.
    fmt 로 출력 포맷 지정 (mp3=브라우저, wav=데스크톱 재생에 편함).
    """
    voice = voice or settings.tts_voice
    instructions = instructions or settings.tts_instructions

    kwargs = {
        "model": settings.tts_model,
        "voice": voice,
        "input": text,
        "response_format": fmt,
    }
    # instructions 는 gpt-4o-*-tts 계열에서만 지원된다.
    if instructions and "gpt-4o" in settings.tts_model:
        kwargs["instructions"] = instructions

    async with openai_client().audio.speech.with_streaming_response.create(**kwargs) as response:
        async for chunk in response.iter_bytes():
            yield chunk
