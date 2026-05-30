from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.config import settings
from app.voice import stt, tts

router = APIRouter(prefix="/voice")

_NO_KEY = "OPENAI_API_KEY 가 설정되지 않아 음성 기능을 쓸 수 없습니다."


@router.post("/stt")
async def speech_to_text(file: UploadFile = File(...), language: str = "ko") -> dict:
    if not settings.has_openai:
        raise HTTPException(503, _NO_KEY)
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "오디오가 비었습니다.")
    text = await stt.transcribe(audio, filename=file.filename or "audio.webm", language=language)
    return {"text": text}


_MEDIA = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}


@router.post("/tts")
async def text_to_speech(payload: dict = Body(...)) -> StreamingResponse:
    if not settings.has_openai:
        raise HTTPException(503, _NO_KEY)
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "text 가 비었습니다.")
    voice = (payload or {}).get("voice")
    instructions = (payload or {}).get("instructions")
    fmt = (payload or {}).get("format", "mp3")
    return StreamingResponse(
        tts.synthesize(text, voice=voice, instructions=instructions, fmt=fmt),
        media_type=_MEDIA.get(fmt, "audio/mpeg"),
    )
