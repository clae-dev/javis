"""자비스 음성 데몬 (PC 상주).

'자비스'라고 부르면 깨어나 명령을 듣고, 백엔드 에이전트로 처리한 뒤 목소리로 답한다.
깨우는 말 감지는 로컬(Vosk)에서만 일어나므로, 깨우기 전에는 음성을 밖으로 보내지 않는다.

준비:
    py -m pip install -r voice_client/requirements.txt
    # 백엔드가 떠 있어야 함:  docker compose up -d
실행:
    py voice_client/jarvis_voice.py
"""

import io
import json
import os
import queue
import random
import sys
import time
import wave
import zipfile
from pathlib import Path

import requests
import sounddevice as sd
import soundfile as sf
import websocket  # websocket-client
from vosk import KaldiRecognizer, Model, SetLogLevel

BACKEND = os.environ.get("JAVIS_BACKEND", "http://localhost:8000")
WS_URL = BACKEND.replace("https", "wss").replace("http", "ws") + "/ws/chat?thread_id=voice"
SAMPLE_RATE = 16000
BLOCK = 4000  # 0.25s 단위

WAKE_WORDS = [
    w.strip()
    for w in os.environ.get("JAVIS_WAKE", "자비스,자비,차비스,장비스,jarvis").split(",")
    if w.strip()
]

DEBUG = bool(os.environ.get("JAVIS_DEBUG"))
INPUT_HINT = os.environ.get("JAVIS_INPUT", "")
OUTPUT_HINT = os.environ.get("JAVIS_OUTPUT", "")


def _resolve_device(hint: str, kind: str):
    """이름 일부로 오디오 장치를 찾는다. 못 찾거나 힌트가 없으면 None(기본 장치)."""
    if not hint:
        return None
    chan = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(sd.query_devices()):
        if dev[chan] > 0 and hint.lower() in dev["name"].lower():
            return i
    return None

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip"
MODEL_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODEL_DIR / "vosk-model-small-ko-0.22"


def ensure_model() -> str:
    if MODEL_PATH.exists():
        return str(MODEL_PATH)
    print("한국어 음성 모델 다운로드 중 (~50MB, 최초 1회)…")
    MODEL_DIR.mkdir(exist_ok=True)
    zip_path = MODEL_DIR / "ko.zip"
    with requests.get(MODEL_URL, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(MODEL_DIR)
    zip_path.unlink()
    print("모델 준비 완료.")
    return str(MODEL_PATH)


def _norm(text: str) -> str:
    return text.replace(" ", "").lower()


def _is_wake(text: str) -> bool:
    t = _norm(text)
    return any(_norm(w) in t for w in WAKE_WORDS)


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class Jarvis:
    def __init__(self, model_path: str) -> None:
        SetLogLevel(-1)
        self.model = Model(model_path)
        self.q: queue.Queue[bytes] = queue.Queue()
        self.in_dev = _resolve_device(INPUT_HINT, "input")
        self.out_dev = _resolve_device(OUTPUT_HINT, "output")
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK,
            dtype="int16",
            channels=1,
            device=self.in_dev,
            callback=self._on_audio,
        )

    def _on_audio(self, indata, frames, time_info, status) -> None:
        self.q.put(bytes(indata))

    def _flush(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    # --- 듣기 ---

    def wait_for_wake(self) -> None:
        rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self._flush()
        last = ""
        while True:
            data = self.q.get()
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
            else:
                text = json.loads(rec.PartialResult()).get("partial", "")
            if DEBUG and text and text != last:
                print(f"  [들림] {text}", flush=True)
                last = text
            if text and _is_wake(text):
                return

    def record_utterance(self, max_seconds: float = 12.0, initial_silence: float = 6.0) -> tuple[bytes, bool]:
        """말이 끝날 때까지 녹음. (오디오, 말했는지) 반환."""
        rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self._flush()
        frames = bytearray()
        spoke = False
        start = time.time()
        while time.time() - start < max_seconds:
            data = self.q.get()
            frames += data
            if rec.AcceptWaveform(data):
                if spoke:  # 말한 뒤 침묵 = 발화 종료
                    break
            else:
                part = json.loads(rec.PartialResult()).get("partial", "")
                if part:
                    spoke = True
            if not spoke and time.time() - start > initial_silence:
                break
        return bytes(frames), spoke

    # --- 백엔드 ---

    def stt(self, pcm: bytes) -> str:
        files = {"file": ("cmd.wav", pcm_to_wav(pcm), "audio/wav")}
        r = requests.post(f"{BACKEND}/voice/stt", files=files, timeout=60)
        r.raise_for_status()
        return r.json().get("text", "").strip()

    def chat(self, text: str) -> str:
        ws = websocket.create_connection(WS_URL, timeout=120)
        ws.send(json.dumps({"content": text}))
        reply = ""
        try:
            while True:
                msg = json.loads(ws.recv())
                kind = msg.get("type")
                if kind == "token":
                    reply += msg.get("content", "")
                elif kind == "tool" and msg.get("status") == "running":
                    print(f"  · {msg.get('name')} …")
                elif kind == "confirm_request":
                    ws.send(json.dumps({"approved": self.confirm(msg.get("data", {}))}))
                elif kind == "done":
                    break
                elif kind == "error":
                    reply = "처리 중에 문제가 생겼어: " + msg.get("message", "")
                    break
        finally:
            ws.close()
        return reply.strip()

    def confirm(self, data: dict) -> bool:
        actions = ", ".join(a.get("name", "") for a in data.get("actions", []))
        self.say(f"{data.get('message', '이 작업을 실행할까요?')} {actions}. 네, 아니오로 답해줘.")
        pcm, spoke = self.record_utterance(max_seconds=6, initial_silence=5)
        if not spoke:
            return False
        ans = _norm(self.stt(pcm))
        yes = any(w in ans for w in ["네", "응", "그래", "좋아", "해줘", "해", "승인", "오케", "ok", "yes", "맞아"])
        no = any(w in ans for w in ["아니", "취소", "하지마", "노", "no", "싫"])
        return yes and not no

    def say(self, text: str) -> None:
        if not text:
            return
        try:
            r = requests.post(
                f"{BACKEND}/voice/tts", json={"text": text, "format": "wav"}, timeout=120
            )
            if r.status_code != 200:
                print("  (음성 합성 실패)", r.text[:200])
                return
            audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
            sd.play(audio, sr, device=self.out_dev)
            sd.wait()
        except Exception as exc:
            print("  (재생 실패)", exc)
        finally:
            self._flush()  # 자기 목소리가 다음 입력에 섞이지 않게

    def beep(self) -> None:
        import numpy as np

        t = np.linspace(0, 0.18, int(SAMPLE_RATE * 0.18), endpoint=False)
        tone = (0.45 * np.sin(2 * np.pi * 660 * t)).astype("float32")
        sd.play(tone, SAMPLE_RATE, device=self.out_dev)
        sd.wait()

    def hud(self, state: str, text: str = "") -> None:
        """HUD 화면에 상태를 흘린다. 실패해도 음성 흐름을 막지 않게 best-effort."""
        try:
            requests.post(f"{BACKEND}/hud/event", json={"state": state, "text": text}, timeout=2)
        except Exception:
            pass

    # --- 메인 루프 ---

    def run(self) -> None:
        try:
            in_name = sd.query_devices(self.in_dev, "input")["name"] if self.in_dev is not None else sd.query_devices(kind="input")["name"]
            out_name = sd.query_devices(self.out_dev, "output")["name"] if self.out_dev is not None else sd.query_devices(kind="output")["name"]
            print(f"입력 장치: {in_name}", flush=True)
            print(f"출력 장치: {out_name}", flush=True)
        except Exception as exc:
            print("장치 조회 실패:", exc, flush=True)
        with self.stream:
            print(f"대기 중… '{WAKE_WORDS[0]}'라고 불러보세요. (종료: Ctrl+C)", flush=True)
            self.hud("idle")
            while True:
                self.wait_for_wake()
                self.beep()
                print("[깨어남] 듣고 있어요…", flush=True)
                self.hud("listening")
                # 깨어났음을 음성으로 알려, 사용자가 이어서 명령하도록 유도.
                self.say(random.choice(["네?", "네, 말씀하세요.", "응? 불렀어요?"]))
                pcm, spoke = self.record_utterance()
                if not spoke:
                    print("…못 들었어요. 다시 대기.", flush=True)
                    self.hud("idle")
                    continue
                try:
                    text = self.stt(pcm)
                except Exception as exc:
                    print("…음성 인식 실패:", exc, flush=True)
                    self.hud("idle")
                    continue
                if not text:
                    print("…내용 없음. 다시 대기.", flush=True)
                    self.hud("idle")
                    continue
                print(f"> {text}", flush=True)
                self.hud("thinking", text)
                try:
                    reply = self.chat(text)
                except Exception as exc:
                    reply = "백엔드에 연결할 수 없어. 자비스 서버가 켜져 있는지 확인해줘."
                    print("…백엔드 오류:", exc, flush=True)
                print(f"자비스: {reply}", flush=True)
                self.hud("speaking", reply)
                self.say(reply)
                self.hud("idle")


def main() -> None:
    try:
        model_path = ensure_model()
    except Exception as exc:
        print("모델 준비 실패:", exc)
        sys.exit(1)

    daemon = Jarvis(model_path)
    try:
        daemon.run()
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
