import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import scheduler
from app.agent.graph import build_graph, make_checkpointer
from app.agent.runtime import runtime
from app.api import hud, rest, voice, ws
from app.config import settings
from app.db.session import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("javis")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncExitStack() as stack:
        checkpointer = await make_checkpointer(stack)
        runtime.graph = build_graph(checkpointer)
        scheduler.start()
        log.info("%s 준비 완료 (OpenAI=%s)", settings.assistant_name, settings.has_openai)
        try:
            yield
        finally:
            scheduler.stop()


app = FastAPI(title="Javis", lifespan=lifespan)

app.include_router(rest.router)
app.include_router(voice.router)
app.include_router(ws.router)
app.include_router(hud.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/hud")
async def hud_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "hud.html")
