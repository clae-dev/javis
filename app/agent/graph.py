import logging
from contextlib import AsyncExitStack

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import tools_condition

from app.agent.nodes import agent, analyze, execute_tools, reflect, retrieve_memory
from app.agent.state import JarvisState
from app.config import settings

log = logging.getLogger("javis.graph")


def _route_after_analyze(state: JarvisState) -> str:
    # 잡담은 기억 조회를 건너뛰고 바로 응답으로. 비용·지연을 아낀다.
    return "agent" if state.get("intent") == "chat" else "retrieve"


def build_graph(checkpointer):
    g = StateGraph(JarvisState)

    g.add_node("analyze", analyze)
    g.add_node("retrieve", retrieve_memory)
    g.add_node("agent", agent)
    g.add_node("tools", execute_tools)
    g.add_node("reflect", reflect)

    g.add_edge(START, "analyze")
    g.add_conditional_edges(
        "analyze",
        _route_after_analyze,
        {"agent": "agent", "retrieve": "retrieve"},
    )
    g.add_edge("retrieve", "agent")

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
    """가능하면 Postgres 영속 체크포인터, 실패하면 인메모리로 폴백.

    Postgres 체크포인터는 재시작 후에도 대화 맥락과 보류 중인 확인을 보존한다.
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
            log.warning("postgres 체크포인터 실패, 메모리로 폴백: %s", exc)
    log.info("checkpointer: memory")
    return MemorySaver()
