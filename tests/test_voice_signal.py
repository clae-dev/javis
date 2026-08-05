"""음성 데몬의 신호 처리 — 포락선과 발화 감지.

둘 다 조용히 틀리는 종류다. 포락선이 어긋나면 HUD 입자가 목소리와 따로 놀고,
발화 감지가 어긋나면 말이 중간에 끊기거나 영영 안 끝난다. 어느 쪽도 예외를
던지지 않아서 로그만 봐서는 모른다.
"""

import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sounddevice", reason="음성 데몬은 sounddevice 가 있어야 불러올 수 있다")
pytest.importorskip("vosk")

_PATH = Path(__file__).resolve().parents[1] / "voice_client" / "jarvis_voice.py"


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location("jarvis_voice", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SR = 24000


def _tone(hz: float, seconds: float = 1.0, amp: float = 0.5):
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


# --- 포락선 ---


def test_silence_is_flat(daemon):
    env = np.array(daemon._envelope(np.zeros(SR, dtype=np.float32), SR))
    assert env.shape[1] == 4          # [세기, 저, 중, 고]
    assert env.max() == 0.0


def test_frame_count_follows_duration(daemon):
    env = daemon._envelope(_tone(440, seconds=2.0), SR)
    expected = int(2.0 * 1000 / daemon.ENVELOPE_FRAME_MS)
    assert abs(len(env) - expected) <= 1


@pytest.mark.parametrize("hz,band", [(150, 1), (1000, 2), (4000, 3)])
def test_each_band_catches_its_own_range(daemon, hz, band):
    """대역이 뒤섞이면 입자가 엉뚱한 소리에 반응한다."""
    env = np.array(daemon._envelope(_tone(hz), SR))
    others = [i for i in (1, 2, 3) if i != band]
    assert env[:, band].mean() > 0.8
    for o in others:
        assert env[:, o].mean() < 0.1


def test_loudness_is_not_normalized_away(daemon):
    """클립마다 최대값으로 맞추면 속삭임이 고함처럼 보인다."""
    loud = np.array(daemon._envelope(_tone(300, amp=0.5), SR))[:, 0].mean()
    soft = np.array(daemon._envelope(_tone(300, amp=0.03), SR))[:, 0].mean()
    assert loud > soft * 3


def test_stereo_is_folded_not_crashed(daemon):
    stereo = np.stack([_tone(300), _tone(300)], axis=1)
    assert len(daemon._envelope(stereo, SR)) > 0


def test_bad_input_returns_empty_instead_of_raising(daemon):
    """포락선 하나 때문에 말을 못 하게 되면 안 된다."""
    assert daemon._envelope(None, SR) == []
    assert daemon._envelope(np.zeros(10, dtype=np.float32), SR) == []


def test_values_stay_in_range(daemon):
    env = np.array(daemon._envelope(_tone(300, amp=0.99), SR))
    assert env.min() >= 0.0 and env.max() <= 1.0


# --- 마이크 세기 ---


def test_block_level_separates_silence_from_sound(daemon):
    quiet = np.zeros(4000, dtype=np.int16).tobytes()
    t = np.linspace(0, 0.25, 4000, endpoint=False)
    loud = ((0.5 * np.sin(2 * np.pi * 300 * t)) * 32767).astype(np.int16).tobytes()

    assert daemon._block_level(quiet) == 0.0
    assert daemon._block_level(loud) > 0.8


def test_block_level_survives_junk(daemon):
    """오디오 한 조각 때문에 녹음이 멈추면 안 된다."""
    assert daemon._block_level(b"") == 0.0
    assert daemon._block_level(b"\x01") == 0.0


def test_level_slot_keeps_only_the_newest(daemon):
    """전송이 밀리면 옛 값이 쌓인다. 늦은 세기는 화면에서 의미가 없으니 버려야 한다."""
    jarvis = daemon.Jarvis.__new__(daemon.Jarvis)
    jarvis._level_slot = None
    jarvis._level_wake = daemon.threading.Event()

    jarvis.hud_level(0.1, 0.0)
    jarvis.hud_level(0.9, 1.0)

    assert jarvis._level_slot == (0.9, 1.0)   # 마지막 것만 남는다
    assert jarvis._level_wake.is_set()


# --- 발화 감지 ---


def _blocks(mono16k):
    """데몬이 다루는 형태(0.25초 int16 블록)로 자른다."""
    pcm = (np.clip(mono16k, -1, 1) * 32767).astype(np.int16)
    return [pcm[i : i + 4000].tobytes() for i in range(0, len(pcm) - 4000, 4000)]


@pytest.fixture(scope="module")
def vad(daemon):
    detector = daemon.VoiceDetector.create()
    if detector is None:
        pytest.skip("VAD 모델을 쓸 수 없는 환경")
    return detector


def test_silence_is_not_speech(daemon, vad):
    vad.reset()
    probs = [vad.feed(b) for b in _blocks(np.zeros(16000 * 2, dtype=np.float32))]
    assert max(probs) < daemon.VAD_THRESHOLD


def test_pure_tone_is_not_speech(daemon, vad):
    """사인파는 소리는 크지만 사람 목소리가 아니다. 음량만 보면 여기서 틀린다."""
    t = np.linspace(0, 2, 16000 * 2, endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    vad.reset()
    probs = [vad.feed(b) for b in _blocks(tone)]
    assert max(probs) < daemon.VAD_THRESHOLD


def test_detector_survives_short_chunks(vad):
    """판정 단위(512샘플)보다 짧은 조각이 와도 죽지 않아야 한다."""
    vad.reset()
    assert vad.feed(np.zeros(100, dtype=np.int16).tobytes()) == 0.0
