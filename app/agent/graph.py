import logging
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import tools_condition

from app.agent.nodes import agent, execute_tools, prepare, reflect
from app.agent.state import JarvisState
from app.config import settings

log = logging.getLogger("javis.graph")


def build_graph(checkpointer):
    g = StateGraph(JarvisState)

    g.add_node("prepare", prepare)
    g.add_node("agent", agent)
    g.add_node("tools", execute_tools)
    g.add_node("reflect", reflect)

    # 분류 LLM 을 임계 경로에서 걷어냈다. 맥락(프로필·기억·감정)을 모은 뒤 바로 응답한다.
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "agent")

    # 도구 호출이 있으면 tools, 없으면 반추 후 종료.
    g.add_conditional_edges(
        "agent",
        tools_condition,
        {"tools": "tools", END: "reflect"},
    )
    g.add_edge("tools", "agent")
    g.add_edge("reflect", END)

    return g.compile(checkpointer=checkpointer)


async def make_checkpointer(stack: AsyncExitStack):
    """대화 맥락과 보류 중인 확인을 재시작 후에도 살려 두는 저장소.

    Postgres → SQLite → 인메모리 순으로 물러난다. 가운데 SQLite 가 낀 이유가 있다.

    백엔드를 윈도우 호스트에서 띄우면 Postgres 체크포인터가 못 붙는다. 그 구현이 쓰는
    psycopg 는 asyncio 의 셀렉터 계열 루프를 요구하는데 윈도우 기본은 Proactor 다.
    그렇다고 셀렉터로 바꾸면 윈도우에서는 서브프로세스를 띄울 수 없어 adb·claude·
    playwright 를 쓰는 도구가 통째로 죽는다 — 체크포인터 하나 살리자고 치르기엔 너무 크다.

    그래서 그 환경에서만 파일 하나로 물러난다. 인메모리와 달리 재시작해도 대화가
    이어지고, 어차피 혼자 쓰는 비서라 조회 성능이 문제 될 일은 없다.
    Docker(리눅스)에서는 이 분기에 걸리지 않고 원래대로 Postgres 를 쓴다.
    """
    if settings.use_postgres_checkpointer:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn)
            )
            await saver.setup()
            log.info("checkpointer: postgres")
            return saver
        except Exception as exc:
            log.warning("postgres 체크포인터 실패, sqlite 로 폴백: %s", exc)

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = Path(settings.checkpoint_sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(path)))
        await saver.setup()
        log.info("checkpointer: sqlite (%s)", path)
        return saver
    except Exception as exc:
        log.warning("sqlite 체크포인터 실패, 메모리로 폴백: %s", exc)

    # 여기까지 오면 재시작 때 대화가 끊긴다. 동작은 하지만 정상은 아니다.
    log.warning("checkpointer: memory — 재시작하면 대화 맥락과 보류 중인 확인이 사라집니다")
    return MemorySaver()
