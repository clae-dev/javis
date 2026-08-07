import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import background, scheduler
from app.agent.graph import build_graph, make_checkpointer
from app.agent.runtime import runtime
from app.api import hud, rest, vision, voice, ws
from app.config import settings
from app.db import audit
from app.db.session import init_db
from app.tools import browser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("javis")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncExitStack() as stack:
        checkpointer = await make_checkpointer(stack)
        runtime.graph = build_graph(checkpointer)
        # 지난번에 돌다 만 작업은 살릴 수 없다. 정리하지 않으면 영원히 '진행 중'으로 남아
        # 사용자가 오지 않을 결과를 기다리게 된다.
        await background.clear_stale()
        await scheduler.start()
        if not settings.javis_token:
            log.warning("JAVIS_TOKEN 이 비어 있습니다 — 인증 없이 열립니다. 이 기계 밖에 노출하지 마세요.")
        log.info("%s 준비 완료 (OpenAI=%s)", settings.assistant_name, settings.has_openai)
        try:
            yield
        finally:
            scheduler.stop()
            # 감사 로그는 묶어서 쓴다. 마지막 묶음이 큐에 남은 채 내려가면 그 기록만 사라진다.
            await audit.flush()
            # 브라우저를 안 닫으면 프로필 폴더 잠금이 남아 다음 기동이 막힌다.
            await browser.close()


app = FastAPI(title="Javis", lifespan=lifespan)

app.include_router(rest.router)
app.include_router(voice.router)
app.include_router(ws.router)
app.include_router(hud.router)
app.include_router(vision.router)
class _RevalidatingStatic(StaticFiles):
    """정적 파일을 캐시하되, 쓰기 전에 서버에 한 번 물어보게 한다.

    기본값으로 두면 브라우저가 옛 CSS·JS 를 그대로 쓴다. 실제로 HUD 를 고친 뒤
    스타일이 안 먹은 화면을 보고 한참 헤맸다 — 코드는 맞는데 화면만 옛것이라
    원인을 짐작하기가 어렵다. no-cache 는 '쓰지 말라'가 아니라 '바뀌었는지
    확인하고 쓰라'는 뜻이라, 안 바뀌었으면 304 로 끝나 비용도 거의 없다.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/hud")
async def hud_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "hud.html")
