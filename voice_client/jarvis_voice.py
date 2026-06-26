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
import threading
import time
import wave
import zipfile
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf
import websocket  # websocket-client
from vosk import KaldiRecognizer, Model, SetLogLevel


def _load_dotenv() -> None:
    """프로젝트 루트 .env 를 읽어 환경변수에 없는 키만 채운다(백엔드와 키 공유).

    의존성 없이 동작하도록 직접 파싱한다. 이미 OS 환경에 있는 값은 덮어쓰지 않는다.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as exc:
        print("(.env 로딩 실패, 무시)", exc)


_load_dotenv()

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

# 박수 두 번 깨우기. '자비스' 음성 호출과 별개로 같이 동작한다.
# 오작동(TV·웃음 등)이 잦으면 PEAK/RATIO 를 올리고, 잘 안 깨면 내린다.
CLAP_ENABLE = os.environ.get("JAVIS_CLAP", "1") != "0"
CLAP_PEAK = float(os.environ.get("JAVIS_CLAP_PEAK", "0.18"))    # 0~1 정규화 절대 피크 임계
CLAP_RATIO = float(os.environ.get("JAVIS_CLAP_RATIO", "4.0"))  # 피크/배경 — 갑작스런 상승 정도
CLAP_GAP_MIN = float(os.environ.get("JAVIS_CLAP_GAP_MIN", "0.08"))  # 두 박수 최소 간격(초)
CLAP_GAP_MAX = float(os.environ.get("JAVIS_CLAP_GAP_MAX", "0.8"))   # 두 박수 최대 간격(초)

# 발화 종료 감지: 말한 뒤 이만큼(초) 인식이 안 자라면 끝난 걸로 본다.
END_SILENCE = float(os.environ.get("JAVIS_END_SILENCE", "0.6"))
# 첫 답변 덩어리는 문장 끝을 안 기다리고 절 경계/최소 길이에서 일찍 말한다.
FIRST_MIN = int(os.environ.get("JAVIS_FIRST_MIN", "4"))    # 첫 덩어리 최소 글자
FIRST_MAX = int(os.environ.get("JAVIS_FIRST_MAX", "20"))   # 절 구분자 없을 때 강제 끊기

# 깨움 응답 후보. 시작 시 미리 합성해 두고 즉시 재생한다.
WAKE_ACKS = ["네?", "네, 말씀하세요.", "응? 불렀어요?"]

# openWakeWord 웨이크워드 엔진. 가입·키 없이 로컬에서 도는 무료 엔진.
# Vosk 는 '자비스' 같은 외래어가 사전에 없어 깸이 불안정해서, 학습된 깸 단어 엔진을 쓴다.
# 'hey_jarvis' 사전학습 모델 내장 → "헤이 자비스 / hey jarvis"로 부른다.
OWW_ENABLE = os.environ.get("JAVIS_OWW", "1") != "0"
OWW_MODEL = os.environ.get("JAVIS_OWW_MODEL", "hey_jarvis")
OWW_THRESHOLD = float(os.environ.get("JAVIS_OWW_THRESHOLD", "0.5"))  # 0~1, 높을수록 엄격


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


_SENTENCE_END = "。.!?…\n"


_CLAUSE_END = ",，、;:"   # 절 경계(쉼표 등) — 첫 덩어리를 일찍 끊을 때만 쓴다


def _sentence_cut(buf: str) -> int:
    """버퍼에서 첫 문장 종결 위치(끝 다음 인덱스)를 반환. 없으면 -1."""
    for i, ch in enumerate(buf):
        if ch in _SENTENCE_END:
            return i + 1
    return -1


def _first_cut(buf: str) -> int:
    """응답 첫 덩어리용. 문장 끝을 기다리지 않고 절 경계/충분한 길이에서 일찍 끊는다.

    첫 음성이 빨리 나오게 하려는 용도. 문장 종결이 먼저 보이면 거기서, 아니면 최소
    길이를 넘긴 뒤 처음 나오는 절 구분자에서, 그것도 없으면 최대 길이에서 단어 경계로.
    """
    s = _sentence_cut(buf)
    if s != -1:
        return s
    for i, ch in enumerate(buf):
        if i + 1 >= FIRST_MIN and ch in _CLAUSE_END:
            return i + 1
    if len(buf) >= FIRST_MAX:
        sp = buf.rfind(" ", 0, FIRST_MAX)
        return sp + 1 if sp >= FIRST_MIN else FIRST_MAX
    return -1


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class ClapDetector:
    """오디오 블록 스트림에서 '박수 두 번'을 감지한다.

    박수는 조용하던 배경에서 갑자기 솟구치는 짧은 소리다. 그래서 (1) 절대 피크가
    충분히 크고, (2) 그 피크가 최근 배경 소음 대비 급격히 높을 때만 박수로 본다.
    배경 추정치(EMA)는 매 순간 갱신되므로, TV·음악처럼 지속적으로 시끄러운 소리는
    배경이 따라 올라가 더 이상 '갑작스러운 상승'으로 잡히지 않는다. 한 번 친 뒤
    정해진 시간 안에 또 한 번 치면 깨움 신호로 본다. 블록(0.25s)을 ~16ms 로 잘게
    쪼개 보기 때문에 빠르게 두 번 친 박수도 놓치지 않는다.
    """

    HOP = 256          # ~16ms. 한 블록 안의 가까운 두 박수도 잡기 위한 분석 단위
    REFRACTORY = 0.12  # 박수 1회의 잔향을 같은 박수로 또 세지 않게 하는 불응 시간(초)
    BG_ALPHA = 0.15    # 배경 소음 EMA 갱신 속도(천천히 따라가게)

    def __init__(self) -> None:
        self._first: float | None = None   # 첫 박수 시각(초)
        self._mute_until = 0.0             # 이 시각 전까지는 새 박수로 안 침
        self._bg = 0.02                    # 배경 rms 추정치
        self._dbg_mute = 0.0               # 디버그 로그 도배 방지

    def feed(self, pcm: bytes, now: float) -> bool:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        for off in range(0, len(samples), self.HOP):
            win = samples[off : off + self.HOP]
            if win.size < 8:
                continue
            peak = float(np.max(np.abs(win)))
            rms = float(np.sqrt(np.mean(win * win)))
            t = now + off / SAMPLE_RATE

            is_clap = (
                peak >= CLAP_PEAK
                and peak >= self._bg * CLAP_RATIO   # 배경 대비 급격한 상승
                and t >= self._mute_until
            )
            # 보정용: 어느 정도 큰 소리(임계 미달 포함)의 실제 레벨을 보여준다.
            if DEBUG and peak >= 0.08 and t >= self._dbg_mute:
                self._dbg_mute = t + 0.08
                ratio = peak / (self._bg + 1e-6)
                print(f"  [소리] peak={peak:.2f} bg={self._bg:.3f} ratio={ratio:.1f}"
                      f"{' ← 박수' if is_clap else ''}", flush=True)
            # 배경 추정은 항상 갱신 — 지속음은 배경이 따라 올라가 더는 안 잡힌다.
            self._bg = (1 - self.BG_ALPHA) * self._bg + self.BG_ALPHA * rms

            if not is_clap:
                continue
            self._mute_until = t + self.REFRACTORY

            if self._first is not None and CLAP_GAP_MIN <= t - self._first <= CLAP_GAP_MAX:
                self._first = None
                return True   # 정해진 간격 안에 두 번째 박수 → 깨움
            self._first = t   # 첫 박수(또는 너무 늦은 두 번째를 새 첫 박수로)

        # 첫 박수만 치고 시간이 지났으면 폐기
        if self._first is not None and now - self._first > CLAP_GAP_MAX:
            self._first = None
        return False


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
        self.oww = self._init_oww()

    def _init_oww(self):
        """openWakeWord 엔진. 비활성/실패면 None(→ Vosk 폴백)."""
        if not OWW_ENABLE:
            return None
        try:
            import openwakeword
            from openwakeword.model import Model as OWWModel

            try:
                openwakeword.utils.download_models([OWW_MODEL])  # 최초 1회 다운로드
            except Exception:
                pass  # 이미 받았거나 네트워크 문제 — 로드 시도는 계속
            oww = OWWModel(wakeword_models=[OWW_MODEL])
            print(f"웨이크워드 엔진: openWakeWord ('{OWW_MODEL}')", flush=True)
            return oww
        except Exception as exc:
            print("openWakeWord 초기화 실패 → Vosk 폴백:", exc, flush=True)
            return None

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
        """깸 신호를 기다린다. Porcupine 이 있으면 그걸로, 없으면 Vosk 로. 박수는 양쪽 병렬."""
        self._flush()
        clap = ClapDetector() if CLAP_ENABLE else None
        if self.oww is not None:
            self._wait_oww(clap)
        else:
            self._wait_vosk(clap)

    def _wait_oww(self, clap: "ClapDetector | None") -> None:
        FRAME = 1280  # 80ms @16kHz — openWakeWord 권장 처리 단위
        leftover = np.empty(0, dtype=np.int16)
        self.oww.reset()  # 직전 버퍼 잔향으로 잘못 깨지 않게 초기화
        while True:
            data = self.q.get()
            if clap is not None and clap.feed(data, time.time()):
                if DEBUG:
                    print("  [박수 감지]", flush=True)
                return
            # 블록(0.25s)을 권장 프레임 단위로 잘라 먹인다.
            leftover = np.concatenate([leftover, np.frombuffer(data, dtype=np.int16)])
            while len(leftover) >= FRAME:
                frame, leftover = leftover[:FRAME], leftover[FRAME:]
                scores = self.oww.predict(frame)
                score = max((v for k, v in scores.items() if "jarvis" in k.lower()), default=0.0)
                if DEBUG and score > 0.3:
                    print(f"  [oww] {score:.2f}", flush=True)
                if score >= OWW_THRESHOLD:
                    if DEBUG:
                        print("  [자비스 감지]", flush=True)
                    self.oww.reset()
                    return

    def _wait_vosk(self, clap: "ClapDetector | None") -> None:
        rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        last = ""
        while True:
            data = self.q.get()
            # 박수 두 번 — '자비스' 음성 호출과 병렬로 감지한다.
            if clap is not None and clap.feed(data, time.time()):
                if DEBUG:
                    print("  [박수 감지]", flush=True)
                return
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
        """말이 끝날 때까지 녹음. (오디오, 말했는지) 반환.

        Vosk 내부 확정만 기다리면 끝이 늦게 잡혀 응답이 느려 보인다. 그래서 부분 인식이
        END_SILENCE 동안 더 안 자라면(=말이 멈춤) 바로 종료한다.
        """
        rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self._flush()
        frames = bytearray()
        spoke = False
        last_part = ""
        start = last_voice = time.time()
        while time.time() - start < max_seconds:
            data = self.q.get()
            frames += data
            now = time.time()
            if rec.AcceptWaveform(data):
                if spoke:  # 말한 뒤 확정 = 발화 종료
                    break
            else:
                part = json.loads(rec.PartialResult()).get("partial", "")
                if part != last_part:   # 인식이 변하는 중 = 아직 말하는 중
                    last_part = part
                    last_voice = now
                    if part:
                        spoke = True
            if not spoke and now - start > initial_silence:
                break
            if spoke and now - last_voice > END_SILENCE:  # 말 멈춘 뒤 짧은 침묵 → 종료
                break
        return bytes(frames), spoke

    # --- 백엔드 ---

    def stt(self, pcm: bytes) -> str:
        files = {"file": ("cmd.wav", pcm_to_wav(pcm), "audio/wav")}
        r = requests.post(f"{BACKEND}/voice/stt", files=files, timeout=60)
        r.raise_for_status()
        return r.json().get("text", "").strip()

    # --- 스트리밍 재생 ---
    #
    # 문장은 두 단계 파이프라인을 거친다: 합성 스레드(_fetcher)가 텍스트를 오디오로
    # 바꿔 audio_q 에 쌓고, 재생 스레드(_player)가 그 오디오를 순서대로 재생한다.
    # 다음 문장 합성이 현재 문장 재생과 겹쳐 돌기 때문에 문장 사이 끊김이 줄고,
    # 응답 전체를 받은 뒤에야 말을 시작하던 기존 방식보다 첫 소리까지가 훨씬 빠르다.

    def _fetcher(self, text_q: "queue.Queue[str | None]", audio_q: "queue.Queue") -> None:
        while True:
            text = text_q.get()
            if text is None:
                audio_q.put(None)  # 재생 스레드도 종료시킨다
                return
            clip = self._tts_fetch(text)
            if clip is not None:
                audio_q.put(clip)

    def _player(self, audio_q: "queue.Queue") -> None:
        while True:
            clip = audio_q.get()
            if clip is None:
                return
            self._tts_play(*clip)

    def _start_player(self) -> "tuple[queue.Queue, list[threading.Thread]]":
        text_q: "queue.Queue[str | None]" = queue.Queue()
        audio_q: "queue.Queue" = queue.Queue()
        threads = [
            threading.Thread(target=self._fetcher, args=(text_q, audio_q), daemon=True),
            threading.Thread(target=self._player, args=(audio_q,), daemon=True),
        ]
        for t in threads:
            t.start()
        return text_q, threads

    @staticmethod
    def _drain_player(text_q: "queue.Queue", threads: "list[threading.Thread]") -> None:
        text_q.put(None)  # 합성→재생 스레드로 종료 신호가 차례로 전파된다
        for t in threads:
            t.join()

    def chat(self, text: str) -> str:
        ws = websocket.create_connection(WS_URL, timeout=120)
        ws.send(json.dumps({"content": text}))
        speak_q, player = self._start_player()
        reply = ""
        buf = ""
        speaking = False
        first_done = False   # 첫 덩어리는 절 단위로 일찍, 이후는 문장 단위로
        try:
            while True:
                msg = json.loads(ws.recv())
                kind = msg.get("type")
                if kind == "token":
                    chunk = msg.get("content", "")
                    reply += chunk
                    buf += chunk
                    if not speaking:
                        self.hud("speaking")
                        speaking = True
                    # 첫 덩어리는 일찍(절 경계), 그 뒤로는 문장 단위로 끊어 합성 큐로.
                    while True:
                        cut = _sentence_cut(buf) if first_done else _first_cut(buf)
                        if cut == -1:
                            break
                        seg = buf[:cut].strip()
                        buf = buf[cut:]
                        if seg:
                            speak_q.put(seg)
                            first_done = True
                elif kind == "tool" and msg.get("status") == "running":
                    print(f"  · {msg.get('name')} …")
                elif kind == "confirm_request":
                    # 확인은 즉시 물어야 한다. 여기까지의 말을 마치고 질문한다.
                    if buf.strip():
                        speak_q.put(buf.strip())
                        buf = ""
                    self._drain_player(speak_q, player)
                    ws.send(json.dumps({"approved": self.confirm(msg.get("data", {}))}))
                    speak_q, player = self._start_player()  # 재개 후 토큰용 새 플레이어
                    speaking = False
                    first_done = False   # 재개 후 답변도 첫 덩어리는 빨리
                elif kind == "done":
                    break
                elif kind == "error":
                    reply = "처리 중에 문제가 생겼어: " + msg.get("message", "")
                    speak_q.put(reply)
                    break
        finally:
            ws.close()
            if buf.strip():
                speak_q.put(buf.strip())
            self._drain_player(speak_q, player)  # 다 말할 때까지 기다린다
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

    def _tts_fetch(self, text: str):
        """문장 하나를 오디오로 합성. (audio, sr) 또는 실패 시 None."""
        try:
            r = requests.post(
                f"{BACKEND}/voice/tts", json={"text": text, "format": "wav"}, timeout=120
            )
            if r.status_code != 200:
                print("  (음성 합성 실패)", r.text[:200])
                return None
            audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
            return audio, sr
        except Exception as exc:
            print("  (합성 실패)", exc)
            return None

    def _tts_play(self, audio, sr) -> None:
        try:
            sd.play(audio, sr, device=self.out_dev)
            sd.wait()
        except Exception as exc:
            print("  (재생 실패)", exc)
        finally:
            self._flush()  # 자기 목소리가 다음 입력에 섞이지 않게

    def say(self, text: str) -> None:
        """한 덩어리 텍스트를 합성해 바로 재생한다 (확인 질문 등 단발 용)."""
        if not text:
            return
        clip = self._tts_fetch(text)
        if clip is not None:
            self._tts_play(*clip)

    def prime_acks(self) -> None:
        """깨움 응답을 시작 시 한 번만 합성해 캐시한다. 이후 깨움은 네트워크 왕복 없이 즉시 재생."""
        self._ack_clips = [c for c in (self._tts_fetch(p) for p in WAKE_ACKS) if c is not None]

    def ack(self) -> None:
        """깨움 응답을 즉시 재생한다. 캐시가 비었으면(합성 실패) 조용히 넘어간다(beep로 충분)."""
        if getattr(self, "_ack_clips", None):
            self._tts_play(*random.choice(self._ack_clips))

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
            self.prime_acks()  # 깨움 응답 미리 합성 (이후 깨움은 즉시 재생)
            wake_word = "헤이 자비스" if self.oww is not None else WAKE_WORDS[0]
            wake_hint = f"'{wake_word}'라고 부르거나 박수 두 번" if CLAP_ENABLE else f"'{wake_word}'라고 불러보세요"
            print(f"대기 중… {wake_hint}. (종료: Ctrl+C)", flush=True)
            self.hud("idle")
            while True:
                self.wait_for_wake()
                self.beep()
                print("[깨어남] 듣고 있어요…", flush=True)
                self.hud("listening")
                # 깨어났음을 음성으로 알린다(미리 합성해둔 응답 → 즉시 재생).
                self.ack()
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
                    reply = self.chat(text)  # 스트리밍 중에 말까지 끝낸다
                except Exception as exc:
                    reply = "백엔드에 연결할 수 없어. 자비스 서버가 켜져 있는지 확인해줘."
                    print("…백엔드 오류:", exc, flush=True)
                    self.say(reply)
                print(f"자비스: {reply}", flush=True)
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
