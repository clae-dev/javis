"""음성 합성.

호출부는 `synthesize()` 하나만 안다 — 텍스트를 넣으면 오디오 청크가 흘러나온다.
어느 제공자를 쓰는지는 여기서만 갈린다. 전체를 모았다가 한 번에 돌려주지 않고
받는 대로 흘려보내기 때문에, 클라이언트는 합성이 끝나기 전에 재생을 시작할 수 있다.

목소리 복제는 OpenAI TTS 로는 안 된다. 복제한 목소리를 쓰려면 TTS_PROVIDER=elevenlabs
로 바꾼다. 키나 voice id 가 비어 있으면 조용히 OpenAI 로 되돌아가므로, 설정을 하다 만
상태에서도 자비스가 벙어리가 되지는 않는다.
"""

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import settings
from app.llm import openai_client

log = logging.getLogger("javis.tts")

_ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech"
# elevenlabs 가 이해하는 출력 포맷 이름. 우리 쪽 fmt 와 이름이 달라 옮겨 준다.
_ELEVEN_FORMAT = {"mp3": "mp3_44100_128", "wav": "pcm_24000"}


def _use_elevenlabs() -> bool:
    return (
        settings.tts_provider.lower() == "elevenlabs"
        and bool(settings.elevenlabs_api_key)
        and bool(settings.elevenlabs_voice_id)
    )


async def _openai(text: str, voice: str, instructions: str | None, fmt: str) -> AsyncIterator[bytes]:
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


async def _elevenlabs(text: str, fmt: str) -> AsyncIterator[bytes]:
    url = f"{_ELEVEN_URL}/{settings.elevenlabs_voice_id}/stream"
    params = {"output_format": _ELEVEN_FORMAT.get(fmt, "mp3_44100_128")}
    payload = {"text": text, "model_id": settings.elevenlabs_model}
    headers = {"xi-api-key": settings.elevenlabs_api_key}

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, params=params, json=payload, headers=headers) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"elevenlabs {r.status_code}: {body[:200]}")
            async for chunk in r.aiter_bytes():
                yield chunk


async def synthesize(
    text: str,
    voice: str | None = None,
    instructions: str | None = None,
    fmt: str = "mp3",
) -> AsyncIterator[bytes]:
    """텍스트를 음성 바이트 청크로 흘려보낸다.

    fmt 로 출력 포맷 지정 (mp3=브라우저, wav=데스크톱 재생에 편함).
    """
    if _use_elevenlabs():
        try:
            async for chunk in _elevenlabs(text, fmt):
                yield chunk
            return
        except Exception as exc:
            # 복제 목소리가 안 나온다고 아예 말을 못 하게 둘 이유는 없다.
            log.warning("elevenlabs 합성 실패, openai 로 폴백: %s", exc)

    async for chunk in _openai(
        text,
        voice or settings.tts_voice,
        instructions or settings.tts_instructions,
        fmt,
    ):
        yield chunk
