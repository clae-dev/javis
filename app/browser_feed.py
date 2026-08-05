"""브라우저 화면을 HUD 로 흘려보낸다.

"지금 뭘 보고 있는지" 를 화면에 띄우는 용도. 자비스가 웹을 뒤지는 동안 사람은 결과
문장만 받아 보는데, 실제로 어느 페이지를 열었는지 눈으로 보이면 훨씬 믿음이 간다.

브라우저는 도구가 쓰는 것과 같은 것을 쓴다(`app/tools/browser.py` 의 영속 컨텍스트).
따로 Chrome 을 디버깅 포트로 띄우지 않아도 되고, 로그인 세션도 그대로 얹힌다.
다만 도구는 호출마다 페이지를 여닫으므로, 피드는 자기 페이지를 하나 들고 있는다.
"""

import asyncio
import base64
import logging

from app.api.hud import hud_manager
from app.tools.browser import _browser

log = logging.getLogger("javis.browserfeed")

# 초당 몇 장. 웹페이지는 영상이 아니라 이 정도면 충분하고, 더 올리면 캡처가 CPU 를 먹는다.
FPS = 2.0
# 가로 폭. HUD 구석에 작게 뜨는 그림이라 원본 해상도를 보낼 이유가 없다.
WIDTH = 720

_task: asyncio.Task | None = None
_page = None
_url = ""


async def _loop() -> None:
    global _page
    interval = 1.0 / FPS
    idle_rounds = 0
    try:
        while True:
            # 보는 사람이 없으면 찍지 않는다. HUD 를 닫아 뒀는데 계속 캡처하면
            # 아무도 안 보는 그림 때문에 CPU 만 돈다.
            if hud_manager.count == 0:
                idle_rounds += 1
                if idle_rounds > 60:      # 30초 넘게 아무도 안 보면 접는다
                    log.info("보는 사람이 없어 브라우저 피드를 멈춥니다")
                    break
                await asyncio.sleep(interval)
                continue
            idle_rounds = 0

            try:
                shot = await _page.screenshot(type="jpeg", quality=60, scale="css")
            except Exception as exc:
                log.warning("화면 캡처 실패: %s", exc)
                break

            await hud_manager.broadcast(
                {
                    "state": "browser",
                    "url": _url,
                    "frame": base64.b64encode(shot).decode("ascii"),
                }
            )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        await _close_page()
        await hud_manager.broadcast({"state": "browser_off"})


async def _close_page() -> None:
    global _page
    if _page is not None:
        try:
            await _page.close()
        except Exception:
            pass
        _page = None


async def start(url: str) -> str:
    """페이지를 열고 화면을 HUD 로 흘리기 시작한다."""
    global _task, _page, _url

    await stop()

    context, err = await _browser()
    if err:
        return err

    try:
        _page = await context.new_page()
        await _page.set_viewport_size({"width": WIDTH, "height": int(WIDTH * 0.62)})
        await _page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        await _close_page()
        return f"페이지를 열지 못했습니다: {exc}"

    _url = url
    _task = asyncio.create_task(_loop())
    return f"{url} 을(를) 화면에 띄웠습니다."


async def stop() -> str:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        return "화면 띄우기를 멈췄습니다."
    await _close_page()
    return "띄워 둔 화면이 없습니다."


def running() -> bool:
    return _task is not None and not _task.done()
