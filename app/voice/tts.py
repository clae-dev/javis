from app.config import settings
from app.llm import openai_client


async def synthesize(text: str, voice: str | None = None, instructions: str | None = None) -> bytes:
    """텍스트를 mp3 음성 바이트로 합성한다 (OpenAI TTS).

    gpt-4o 계열 TTS 모델은 instructions 로 말투·감정을 조절할 수 있다.
    """
    voice = voice or settings.tts_voice
    instructions = instructions or settings.tts_instructions

    kwargs = {
        "model": settings.tts_model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    # instructions 는 gpt-4o-*-tts 계열에서만 지원된다.
    if instructions and "gpt-4o" in settings.tts_model:
        kwargs["instructions"] = instructions

    async with openai_client().audio.speech.with_streaming_response.create(**kwargs) as response:
        return await response.read()
