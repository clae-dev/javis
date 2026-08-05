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
            # 브라우저를 안 닫으면 프로필 폴더 잠금이 남아 다음 기동이 막힌다.
            await browser.close()


app = FastAPI(title="Javis", lifespan=lifespan)

app.include_router(rest.router)
app.include_router(voice.router)
app.include_router(ws.router)
app.include_router(hud.router)
app.include_router(vision.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/hud")
async def hud_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "hud.html")
