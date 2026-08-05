"""브라우저 화면 피드.

초당 몇 장씩 캡처하는 루프라, 아무도 안 보는데 계속 돌면 조용히 CPU 만 먹는다.
눈에 띄지 않는 종류의 낭비라 여기서 못박아 둔다.
"""

import asyncio

import pytest

from app import browser_feed


class _Page:
    def __init__(self) -> None:
        self.shots = 0
        self.closed = False

    async def screenshot(self, **kw):
        self.shots += 1
        return b"\xff\xd8fake-jpeg"

    async def close(self):
        self.closed = True

    async def set_viewport_size(self, size):
        pass

    async def goto(self, url, **kw):
        pass


@pytest.fixture
def feed(monkeypatch):
    """캡처를 빠르게 돌리고 브로드캐스트를 가로챈다."""
    page = _Page()
    sent: list[dict] = []
    watchers = {"n": 1}

    async def fake_broadcast(msg):
        sent.append(msg)

    class _Manager:
        @property
        def count(self):
            return watchers["n"]

        broadcast = staticmethod(fake_broadcast)

    monkeypatch.setattr(browser_feed, "hud_manager", _Manager())
    monkeypatch.setattr(browser_feed, "FPS", 200.0)  # 테스트가 기다리지 않게
    monkeypatch.setattr(browser_feed, "_page", page)
    return page, sent, watchers


async def test_frames_go_out_while_someone_watches(feed):
    page, sent, _ = feed
    task = asyncio.create_task(browser_feed._loop())
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert page.shots > 0
    frames = [m for m in sent if m.get("state") == "browser"]
    assert frames and frames[0]["frame"], "프레임이 실려 나가야 한다"


async def test_capture_stops_when_nobody_is_watching(feed):
    """HUD 를 닫아 뒀는데 계속 찍으면 아무도 안 보는 그림에 CPU 를 쓴다."""
    page, _, watchers = feed
    watchers["n"] = 0

    task = asyncio.create_task(browser_feed._loop())
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert page.shots == 0


async def test_page_is_closed_when_the_loop_ends(feed):
    """페이지를 안 닫으면 브라우저에 탭이 쌓인다."""
    page, sent, _ = feed
    task = asyncio.create_task(browser_feed._loop())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert page.closed
    assert any(m.get("state") == "browser_off" for m in sent), "화면을 내리라고 알려야 한다"
