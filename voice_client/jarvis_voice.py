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
import tempfile
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
_WS_BASE = BACKEND.replace("https", "wss").replace("http", "ws")
WS_URL = _WS_BASE + "/ws/chat?thread_id=voice"
# 능동 알림만 듣는 별도 연결. 대화 소켓은 턴 사이에 아무도 읽지 않아서 알림이 갇힌다.
WS_URL_NOTIFY = _WS_BASE + "/ws/chat?thread_id=voice-notify"
NOTIFY_TIMEOUT = 60          # recv 타임아웃(초). 지나면 연결 확인만 하고 다시 듣는다.
NOTIFY_WAIT_MAX = 120        # 대화가 끝나기를 이만큼(초) 기다렸다가 포기한다.

# 백엔드에 JAVIS_TOKEN 이 설정돼 있으면 같은 값을 여기도 넣는다. 비어 있으면 인증 없음.
TOKEN = os.environ.get("JAVIS_TOKEN", "").strip()
AUTH_HEADERS = {"X-Javis-Token": TOKEN} if TOKEN else {}
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

# 답변이 끝난 뒤 이만큼(초)은 깨우지 않고 바로 이어 말할 수 있다. 0 이면 매번 다시 불러야 한다.
CONV_TIMEOUT = float(os.environ.get("JAVIS_CONV_TIMEOUT", "10"))

# 운전 모드. 백엔드가 답변을 두세 문장으로 줄인다.
DRIVE_MODE = os.environ.get("JAVIS_DRIVE", "0") != "0"

# 첫 덩어리를 로컬 음성(Windows SAPI)으로 즉시 내보낼지. 클라우드 왕복이 빠져 첫 소리가
# 빨라지지만, 첫 문장만 목소리가 달라진다. 목소리로 인격을 만드는 이상 이 불일치는
# 책상 앞에서 제일 거슬리는 지점이다 — 반대로 차 안에서는 지연이 크고 노면 소음이
# 차이를 덮어서 이득만 남는다. 그래서 기본은 auto (운전 모드에서만 켬).
#   auto(기본) / 1(항상) / 0(끔)
_first_local = os.environ.get("JAVIS_TTS_FIRST_LOCAL", "auto").strip().lower()
TTS_FIRST_LOCAL = DRIVE_MODE if _first_local == "auto" else _first_local != "0"

# 백엔드 웹소켓은 한 번 붙여 두고 계속 쓴다. 다만 이만큼(초) 놀린 연결은 NAT·방화벽이
# 조용히 끊어 놓았을 수 있어, 쓰기 전에 새로 붙는다. 죽은 소켓에 보내면 살아 있는 줄
# 알고 응답을 기다리다 타임아웃까지 통째로 날린다.
WS_IDLE_MAX = float(os.environ.get("JAVIS_WS_IDLE_MAX", "120"))


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


_KOREAN_VOICE_HINTS = ("korean", "한국")


class _LocalTTS:
    """Windows SAPI 합성기를 전용 스레드 하나에 가둬 두고 재사용한다.

    합성 자체는 빠른데 준비가 비쌌다. 발화마다 CoInitialize → SAPI.SpVoice 생성 →
    설치된 음성 전체 열거를 다시 하면 수백 ms 가 붙는다 — 하필 '첫 소리를 앞당기려고'
    로컬 합성을 쓰는 구간이라 벌어 온 시간을 그대로 까먹는다.

    COM 객체는 만든 스레드에서만 안전하게 쓸 수 있어서, 스레드를 하나 띄워 거기서
    딱 한 번 준비하고 이후에는 텍스트만 넘긴다. 요청 큐로 받고 결과는 일회용 큐로
    돌려준다.
    """

    def __init__(self) -> None:
        self._jobs: "queue.Queue[tuple[str, queue.Queue]]" = queue.Queue()
        self._ready = threading.Event()
        self._voice = None
        self._ok = False
        threading.Thread(target=self._loop, daemon=True).start()

    def warm(self, timeout: float = 10.0) -> bool:
        """준비가 끝날 때까지 기다린다. 이 기계에서 못 쓰면 False."""
        self._ready.wait(timeout)
        return self._ok

    def synth(self, text: str, timeout: float = 10.0):
        """(오디오, 샘플레이트) 또는 실패 시 None. 호출부는 None 이면 클라우드로 간다."""
        if not self._ok:
            return None
        box: "queue.Queue" = queue.Queue(maxsize=1)
        self._jobs.put((text, box))
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return None

    def _loop(self) -> None:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            # 기본 음성이 영어면 한국어를 이상하게 읽는다. 한국어 음성이 있으면 그걸 쓴다.
            for token in self._voice.GetVoices():
                if any(h in token.GetDescription().lower() for h in _KOREAN_VOICE_HINTS):
                    self._voice.Voice = token
                    break
            self._ok = True
        except Exception as exc:
            if DEBUG:
                print("  (로컬 합성 사용 불가 → 클라우드)", exc, flush=True)
        finally:
            self._ready.set()

        if not self._ok:
            return

        while True:
            text, box = self._jobs.get()
            box.put(self._speak(text))

    def _speak(self, text: str):
        # 메모리 스트림 대신 임시 파일을 거친다. SAPI 메모리 스트림은 헤더 없는 PCM 이
        # 나와 포맷을 손으로 맞춰야 하는데, 짧은 문장 파일 하나 쓰고 읽는 비용은 어차피
        # 위에서 걷어낸 COM 준비 비용에 비하면 없는 것과 같다.
        import win32com.client

        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(tmp, 3)  # 3 = 쓰기용으로 새로 만들기
            self._voice.AudioOutputStream = stream
            self._voice.Speak(text)
            stream.Close()
            return sf.read(tmp, dtype="float32")
        except Exception as exc:
            if DEBUG:
                print("  (로컬 합성 실패 → 클라우드)", exc, flush=True)
            return None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


_local_tts: "_LocalTTS | None" = None


def _start_local_tts() -> None:
    """로컬 합성기를 미리 띄운다. 첫 발화가 COM 초기화를 기다리지 않게 한다."""
    global _local_tts
    if os.name != "nt" or not TTS_FIRST_LOCAL or _local_tts is not None:
        return
    _local_tts = _LocalTTS()
    if _local_tts.warm():
        # 객체를 만드는 것과 별개로, 첫 Speak 에도 한 번뿐인 준비 비용이 붙는다
        # (실측 200ms 남짓). 짧은 문장 하나를 버리는 셈 치고 합성해 그것까지 여기서
        # 치른다. 재생은 하지 않으니 아무 소리도 나지 않는다.
        _local_tts.synth("네")


def _tts_local(text: str):
    """Windows SAPI 로 즉시 합성해 (오디오, 샘플레이트) 를 준다. 못 쓰면 None.

    클라우드 왕복(수백 ms)이 통째로 빠지므로 답변 첫 덩어리만 여기로 보내 첫 소리를
    앞당긴다. 목소리 품질은 클라우드가 낫기 때문에 나머지 문장은 그쪽으로 간다.
    """
    _start_local_tts()  # 미리 안 띄웠으면 여기서 한 번(이후 호출은 그냥 통과)
    return _local_tts.synth(text) if _local_tts is not None else None


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
        self.ws: "websocket.WebSocket | None" = None
        self._ws_used = 0.0
        # 스피커는 하나뿐이다. 알림 스레드와 응답 재생이 동시에 울리지 않게 직렬화한다.
        self._speak_lock = threading.Lock()
        # 대화 한 턴을 처리하는 동안 켜 둔다. 알림은 이게 꺼질 때까지 기다렸다 말한다.
        self._busy = threading.Event()
        self.running = True

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
        r = requests.post(f"{BACKEND}/voice/stt", files=files, headers=AUTH_HEADERS, timeout=60)
        r.raise_for_status()
        return r.json().get("text", "").strip()

    # --- 스트리밍 재생 ---
    #
    # 문장은 두 단계 파이프라인을 거친다: 합성 스레드(_fetcher)가 텍스트를 오디오로
    # 바꿔 audio_q 에 쌓고, 재생 스레드(_player)가 그 오디오를 순서대로 재생한다.
    # 다음 문장 합성이 현재 문장 재생과 겹쳐 돌기 때문에 문장 사이 끊김이 줄고,
    # 응답 전체를 받은 뒤에야 말을 시작하던 기존 방식보다 첫 소리까지가 훨씬 빠르다.

    def _fetcher(self, text_q: "queue.Queue[str | None]", audio_q: "queue.Queue") -> None:
        first = True
        while True:
            text = text_q.get()
            if text is None:
                audio_q.put(None)  # 재생 스레드도 종료시킨다
                return
            # 첫 덩어리만 로컬에서 합성해 첫 소리를 앞당긴다. 실패하면 그냥 클라우드로.
            clip = _tts_local(text) if (first and TTS_FIRST_LOCAL) else None
            if clip is None:
                clip = self._tts_fetch(text)
            first = False
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

    def _connect(self) -> "websocket.WebSocket":
        """백엔드 웹소켓을 준비한다. 살아 있으면 쓰던 걸 그대로 쓴다.

        발화마다 새로 열면 턴마다 TCP·웹소켓 핸드셰이크와 인증 프레임이 앞에 붙는다.
        이어 말하기(follow-up)로 10초마다 오가는 상황이나 원격으로 붙어 있을 때는
        이 왕복이 그대로 첫 응답 지연이 된다.
        """
        ws = self.ws
        if ws is not None and time.time() - self._ws_used <= WS_IDLE_MAX:
            try:
                if ws.connected:
                    return ws
            except Exception:
                pass
        self._close_ws()

        ws = websocket.create_connection(WS_URL, timeout=120)
        if TOKEN:
            ws.send(json.dumps({"token": TOKEN}))  # 첫 프레임이 인증
        self.ws = ws
        return ws

    def _close_ws(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def chat(self, text: str) -> str:
        payload = {"content": text}
        if DRIVE_MODE:
            payload["mode"] = "drive"  # 백엔드가 답변을 두세 문장으로 줄인다
        body = json.dumps(payload)

        # 다시 붙는 건 '보내기까지'만이다. 토큰이 오기 시작한 뒤에 재시도하면 이미 말한
        # 문장을 한 번 더 말하게 된다.
        for last_try in (False, True):
            try:
                ws = self._connect()
                ws.send(body)
                break
            except Exception:
                self._close_ws()
                if last_try:
                    raise

        return self._converse(ws)

    def _converse(self, ws: "websocket.WebSocket") -> str:
        """한 번의 응답을 끝까지 받아 말한다. 받은 전체 텍스트를 돌려준다."""
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
                # proactive 는 일부러 흘려보낸다. 능동 알림은 전용 리스너가 맡는다 —
                # 여기서도 처리하면 같은 알림을 두 번 말하게 된다.
            self._ws_used = time.time()  # 끝까지 정상으로 받았다 = 다음 턴에 재사용 가능
        except Exception:
            self._close_ws()  # 중간에 끊긴 연결은 상태가 어긋나 있다. 다음 턴에 새로 붙는다.
            raise
        finally:
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
                f"{BACKEND}/voice/tts",
                json={"text": text, "format": "wav"},
                headers=AUTH_HEADERS,
                timeout=120,
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
        # 알림 스레드가 끼어들어 응답 위에 겹쳐 울리는 걸 막는다.
        with self._speak_lock:
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
        """시작 시 한 번만 준비해 두는 것들.

        깨움 응답은 미리 합성해 두면 이후 깨움이 네트워크 왕복 없이 즉시 나간다.
        로컬 합성기도 여기서 띄워, 첫 답변이 COM 초기화를 기다리지 않게 한다.
        """
        _start_local_tts()
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

    # --- 능동 알림 ---
    #
    # 맡겨 둔 작업이 끝나거나 리마인더가 걸리면 백엔드가 연결된 모든 채팅 소켓으로
    # 알림을 밀어 준다. 문제는 턴 사이에 그 소켓을 아무도 읽지 않는다는 것 — 다음에
    # 말을 걸 때까지 알림이 버퍼에 갇힌다. 그래서 듣기만 하는 연결을 따로 하나 둔다.
    # (대화 소켓에도 같은 알림이 오지만 _converse 는 그냥 흘려보낸다. 양쪽에서 처리하면
    #  같은 알림을 두 번 말하게 된다.)

    def _notify_listener(self) -> None:
        while self.running:
            try:
                ws = websocket.create_connection(WS_URL_NOTIFY, timeout=NOTIFY_TIMEOUT)
                if TOKEN:
                    ws.send(json.dumps({"token": TOKEN}))
            except Exception:
                time.sleep(5)
                continue

            try:
                while self.running:
                    try:
                        msg = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue  # 조용한 시간. 연결은 살아 있다.
                    if (msg or {}).get("type") != "proactive":
                        continue
                    content = str(msg.get("content") or "").strip()
                    if content:
                        self._announce(content)
            except Exception:
                pass  # 끊겼다. 위에서 다시 붙는다.
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            time.sleep(3)

    def _announce(self, content: str) -> None:
        """알림을 말한다. 대화 중이면 끝날 때까지 기다린다."""
        print(f"\n[알림] {content}", flush=True)
        # 말하는 중에 끼어들면 사용자 대화를 덮어쓴다. 잠깐이면 기다리고, 너무 길어지면
        # 화면에는 이미 찍혔으니 음성은 포기한다.
        for _ in range(int(NOTIFY_WAIT_MAX)):
            if not self._busy.is_set():
                break
            time.sleep(1)
        else:
            return
        self.say(content)

    def hud(self, state: str, text: str = "") -> None:
        """HUD 화면에 상태를 흘린다. 실패해도 음성 흐름을 막지 않게 best-effort."""
        try:
            requests.post(
                f"{BACKEND}/hud/event",
                json={"state": state, "text": text},
                headers=AUTH_HEADERS,
                timeout=2,
            )
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
            if DRIVE_MODE:
                print("운전 모드: 답변을 짧게 합니다.", flush=True)
            # 첫 문장 목소리가 달라지는 건 눈치채기 어렵고 원인 찾기는 더 어렵다. 밝혀 둔다.
            print(
                "첫 덩어리 합성: "
                + ("로컬(빠름, 첫 문장만 목소리 다름)" if TTS_FIRST_LOCAL else "클라우드(목소리 일관)"),
                flush=True,
            )
            self.hud("idle")
            # 맡겨 둔 작업이 끝나거나 리마인더가 걸리면 여기로 온다. 대기 중에도 듣는다.
            threading.Thread(target=self._notify_listener, daemon=True).start()
            # 방금 답을 마쳤으면 잠깐은 깨우지 않고 이어 말할 수 있다(시리와 같은 방식).
            follow_up = False
            while True:
                self._busy.clear()  # 여기서부터는 조용하다. 알림이 끼어들어도 된다.
                if follow_up:
                    # 깨움 신호도 띵 소리도 없이 곧장 듣는다. 침묵하면 다시 대기로 돌아간다.
                    pcm, spoke = self.record_utterance(
                        max_seconds=CONV_TIMEOUT + 12, initial_silence=CONV_TIMEOUT
                    )
                    if not spoke:
                        print("…이어지는 말 없음. 다시 대기.", flush=True)
                        follow_up = False
                        self.hud("idle")
                        continue
                else:
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
                follow_up = False  # 아래에서 답을 마쳤을 때만 다시 켠다
                self._busy.set()   # 여기부터 답을 마칠 때까지 알림은 기다린다
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
                if CONV_TIMEOUT > 0:
                    follow_up = True
                    print(f"(계속 말해도 됩니다 · {CONV_TIMEOUT:.0f}초)", flush=True)
                    self.hud("listening")
                else:
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
    finally:
        daemon.running = False  # 알림 리스너도 같이 내려간다
        daemon._close_ws()


if __name__ == "__main__":
    main()
