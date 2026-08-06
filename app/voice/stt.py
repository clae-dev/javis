import asyncio
import io
import logging

from app.config import settings
from app.llm import openai_client

log = logging.getLogger("javis.stt")

_local_model = None


def _load_local():
    """faster-whisper 모델을 한 번만 올린다. 최초 호출 때 모델을 내려받는다(수백 MB)."""
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel

        log.info("로컬 STT 모델 로드 중: %s (최초 1회 다운로드)", settings.stt_local_model)
        # int8 양자화 CPU 실행 — GPU 없이도 실시간에 가깝고 메모리를 덜 쓴다.
        _local_model = WhisperModel(settings.stt_local_model, device="cpu", compute_type="int8")
    return _local_model


def _transcribe_local(audio: bytes, language: str) -> str:
    segments, _ = _load_local().transcribe(
        io.BytesIO(audio),
        language=language,
        beam_size=settings.stt_local_beam,
        vad_filter=False,
    )
    return "".join(seg.text for seg in segments).strip()


async def _transcribe_cloud(audio: bytes, filename: str, language: str) -> str:
    buffer = io.BytesIO(audio)
    buffer.name = filename
    result = await openai_client().audio.transcriptions.create(
        model=settings.stt_model,
        file=buffer,
        language=language,
    )
    return result.text


async def transcribe(audio: bytes, filename: str = "audio.webm", language: str = "ko") -> str:
    """음성 바이트를 텍스트로 옮긴다.

    STT_ENGINE=local 이면 이 기계에서 직접 받아쓴다 — 차 안처럼 네트워크가 흔들리는 곳에서
    왕복이 사라지는 게 크다. 로컬이 실패하면 클라우드로 넘어가므로 못 알아듣는 일은 없다.
    """
    if settings.stt_engine == "local":
        try:
            return await asyncio.to_thread(_transcribe_local, audio, language)
        except Exception as exc:
            log.warning("로컬 STT 실패, 클라우드로 폴백: %s", exc)

    return await _transcribe_cloud(audio, filename, language)
