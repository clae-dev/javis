"""비전 데몬의 순수 로직.

데몬은 패키지가 아니라 단독 스크립트라 파일에서 직접 불러온다. 카메라도 모델 파일도
필요 없는 부분만 본다 — 제스처 판정과 임베딩 정규화.
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("cv2", reason="비전 데몬은 opencv-python 이 있어야 불러올 수 있다")

np = pytest.importorskip("numpy")

_PATH = Path(__file__).resolve().parents[1] / "voice_client" / "jarvis_vision.py"


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location("jarvis_vision", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 제스처 판정 ---


@pytest.mark.parametrize(
    "dx,dy,expected",
    [
        (0.30, 0.02, "spread"),  # 두 손을 벌림
        (-0.30, 0.02, "gather"),  # 두 손을 모음
        (0.02, 0.30, "push_down"),  # 두 손을 내림 (y 는 아래로 증가)
        (0.02, -0.30, "pull_up"),  # 두 손을 올림
    ],
)
def test_clear_motions_are_classified(daemon, dx, dy, expected):
    assert daemon.classify_motion(dx, dy, 0.20) == expected


@pytest.mark.parametrize("dx,dy", [(0.0, 0.0), (0.1, 0.1), (0.19, 0.05), (0.05, 0.19)])
def test_small_movements_do_not_fire(daemon, dx, dy):
    """손을 조금 움직였다고 발동하면 카메라 앞에서 아무것도 못 한다."""
    assert daemon.classify_motion(dx, dy, 0.20) is None


def test_dominant_axis_wins(daemon):
    """비스듬히 움직이면 더 큰 축 하나만 나가야 한다."""
    assert daemon.classify_motion(0.40, 0.25, 0.20) == "spread"
    assert daemon.classify_motion(0.25, 0.40, 0.20) == "push_down"


def test_every_gesture_has_a_media_key(daemon):
    """제스처를 늘리고 매핑을 빠뜨리면 조용히 아무 일도 안 일어난다."""
    for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        name = daemon.classify_motion(dx, dy, 0.2)
        assert name in daemon._MEDIA_KEYS, name


# --- 임베딩 ---


def test_normalize_gives_unit_length(daemon):
    vec = daemon._normalize(np.array([3.0, 4.0], dtype=np.float32))
    assert float(np.linalg.norm(vec)) == pytest.approx(1.0)


def test_normalize_survives_zero_vector(daemon):
    """검출은 됐는데 특징이 0으로 나오는 프레임이 있다. 0으로 나누면 안 된다."""
    vec = daemon._normalize(np.zeros(4, dtype=np.float32))
    assert not np.isnan(vec).any()


def test_cosine_of_normalized_vectors_is_the_dot_product(daemon):
    a = daemon._normalize(np.array([1.0, 1.0], dtype=np.float32))
    b = daemon._normalize(np.array([1.0, 0.0], dtype=np.float32))
    assert float(np.dot(a, b)) == pytest.approx(0.7071, abs=1e-3)


def test_threshold_matches_the_server_dimension(daemon):
    """데몬이 만드는 임베딩 차원과 서버 컬럼 차원이 어긋나면 등록이 400 으로 막힌다."""
    from app.config import settings

    assert settings.face_embedding_dim == 128  # OpenCV SFace 출력
