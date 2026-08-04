"""카메라 계층.

DB 없이 검증할 수 있는 건 캡처 프로토콜(요청-응답, 시간 초과, 잘못된 이미지)과
인식 결과의 신선도 판단이다. 얼굴 등록·조회는 DB 를 타므로 여기서 다루지 않는다.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.api import vision
from app.tools import vision as tools


@pytest.fixture(autouse=True)
def clean_channel():
    """모듈 전역 상태를 테스트마다 되돌린다."""
    vision._client = None
    vision._latest = None
    vision._pending = None
    yield
    vision._client = None
    vision._latest = None
    vision._pending = None


class FakeSocket:
    """서버가 보낸 요청을 받아 두고, 미리 정한 응답을 흘려 넣는다."""

    def __init__(self, reply=None, delay: float = 0.0):
        self.sent: list[dict] = []
        self._reply = reply
        self._delay = delay

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        if self._reply is None:
            return

        async def answer():
            await asyncio.sleep(self._delay)
            if vision._pending is not None and not vision._pending.done():
                vision._pending.set_result(self._reply)

        asyncio.get_running_loop().create_task(answer())


# --- 캡처 프로토콜 ---


async def test_capture_without_daemon_returns_hint():
    frame, err = await vision.capture()
    assert frame is None and err == vision.SETUP_HINT


async def test_capture_asks_the_daemon_and_returns_bytes():
    vision._client = FakeSocket(reply=b"\xff\xd8jpeg")
    frame, err = await vision.capture()

    assert err is None
    assert frame == b"\xff\xd8jpeg"
    assert vision._client.sent == [{"type": "capture"}]


async def test_capture_times_out(monkeypatch):
    monkeypatch.setattr(vision, "CAPTURE_TIMEOUT", 0.05)
    vision._client = FakeSocket(reply=None)  # 아무 응답도 안 온다

    frame, err = await vision.capture()
    assert frame is None and "제때 응답하지" in err


async def test_capture_clears_pending_after_timeout(monkeypatch):
    """대기 상태가 남으면 다음 캡처가 남의 응답을 받는다."""
    monkeypatch.setattr(vision, "CAPTURE_TIMEOUT", 0.05)
    vision._client = FakeSocket(reply=None)
    await vision.capture()
    assert vision._pending is None


async def test_capture_passes_daemon_error_through():
    vision._client = FakeSocket(reply="카메라를 열지 못했습니다.")
    frame, err = await vision.capture()
    assert frame is None and err == "카메라를 열지 못했습니다."


async def test_concurrent_captures_do_not_cross_wires():
    """요청이 겹쳐도 각자 자기 응답을 받아야 한다."""
    vision._client = FakeSocket(reply=b"frame", delay=0.01)
    results = await asyncio.gather(vision.capture(), vision.capture())
    assert [frame for frame, _ in results] == [b"frame", b"frame"]
    assert len(vision._client.sent) == 2


# --- 프레임 해석 ---


def test_frame_must_be_valid_base64():
    assert isinstance(vision._resolve_frame({"image": "이건 base64 가 아니다!!"}), str)


def test_empty_frame_is_rejected():
    assert isinstance(vision._resolve_frame({"image": ""}), str)


def test_oversized_frame_is_rejected(monkeypatch):
    import base64

    monkeypatch.setattr(vision, "MAX_FRAME_BYTES", 16)
    payload = {"image": base64.b64encode(b"x" * 64).decode()}
    assert isinstance(vision._resolve_frame(payload), str)


def test_valid_frame_decodes():
    import base64

    payload = {"image": base64.b64encode(b"\xff\xd8jpeg").decode()}
    assert vision._resolve_frame(payload) == b"\xff\xd8jpeg"


# --- 등록된 얼굴 직렬화 ---


def test_embeddings_serialize_as_plain_floats():
    """pgvector 는 numpy 배열을 돌려준다. numpy.float32 는 JSON 으로 못 나가고,
    그대로 두면 목록 조회가 500 으로 죽는다 — 데몬이 얼굴을 하나도 못 받는다."""
    import json

    np = pytest.importorskip("numpy")
    values = vision._floats(np.array([0.1, 0.2, 0.3], dtype=np.float32))

    assert all(type(v) is float for v in values)
    json.dumps(values)  # 여기서 터지면 엔드포인트도 터진다


# --- 누가 보이는지 ---


def _seen(names, unknown=0, age_seconds=0):
    return vision.Sighting(
        names=names,
        unknown=unknown,
        at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


def test_fresh_sighting_is_returned():
    vision._latest = _seen(["창래"])
    assert vision.latest_sighting().names == ["창래"]


def test_stale_sighting_is_ignored():
    """한참 전에 본 사람을 '지금 있다'고 답하면 안 된다."""
    vision._latest = _seen(["창래"], age_seconds=vision.SIGHTING_TTL + 5)
    assert vision.latest_sighting() is None


async def test_who_is_here_without_daemon():
    assert await tools.who_is_here.ainvoke({}) == vision.SETUP_HINT


async def test_who_is_here_names_people():
    vision._client = FakeSocket()
    vision._latest = _seen(["창래", "아들"])
    result = await tools.who_is_here.ainvoke({})
    assert "창래" in result and "아들" in result


async def test_who_is_here_counts_strangers():
    vision._client = FakeSocket()
    vision._latest = _seen([], unknown=2)
    assert "2명" in await tools.who_is_here.ainvoke({})


async def test_who_is_here_when_empty():
    vision._client = FakeSocket()
    vision._latest = _seen([], unknown=0)
    assert "아무도" in await tools.who_is_here.ainvoke({})


async def test_who_is_here_when_stale():
    vision._client = FakeSocket()
    vision._latest = _seen(["창래"], age_seconds=999)
    assert "아무도" in await tools.who_is_here.ainvoke({})


# --- look ---


async def test_look_without_daemon_returns_hint(monkeypatch):
    monkeypatch.setattr(tools.settings, "openai_api_key", "test-key")
    assert await tools.look.ainvoke({}) == vision.SETUP_HINT


async def test_look_without_api_key(monkeypatch):
    monkeypatch.setattr(tools.settings, "openai_api_key", "")
    assert "OPENAI_API_KEY" in await tools.look.ainvoke({})


async def test_look_sends_the_frame_as_an_image_block(monkeypatch):
    monkeypatch.setattr(tools.settings, "openai_api_key", "test-key")
    vision._client = FakeSocket(reply=b"\xff\xd8jpeg-bytes")
    captured = {}

    class _Model:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return type("R", (), {"content": "책상 위에 커피잔이 있습니다."})()

    monkeypatch.setattr(tools, "chat", lambda *a, **k: _Model())

    result = await tools.look.ainvoke({"question": "이거 뭐야"})
    assert result == "책상 위에 커피잔이 있습니다."

    blocks = captured["messages"][-1].content
    assert blocks[0] == {"type": "text", "text": "이거 뭐야"}
    assert blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_look_reports_model_failure(monkeypatch):
    monkeypatch.setattr(tools.settings, "openai_api_key", "test-key")
    vision._client = FakeSocket(reply=b"jpeg")

    class _Model:
        async def ainvoke(self, _messages):
            raise RuntimeError("rate limited")

    monkeypatch.setattr(tools, "chat", lambda *a, **k: _Model())
    assert "rate limited" in await tools.look.ainvoke({})
