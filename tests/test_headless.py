"""사람 없이 그래프를 태우는 통로.

정기 작업과 백그라운드 작업이 같이 쓴다. 둘 다 승인해 줄 사람이 없는 자리에서 도는데,
쓰기 도구를 만나면 그래프가 확인을 요구하며 멈춘다. 그때 조용히 실행돼 버리거나
반대로 영영 매달려 있으면 곤란하다 — 취소로 정리하고, 무엇이 막혔는지 말해 줘야 한다.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.headless import Outcome, _answer_of, run_prompt


# --- 답변 뽑기 ---


def test_answer_of_picks_last_ai_message():
    state = {
        "messages": [
            HumanMessage(content="확인해줘"),
            AIMessage(content="", tool_calls=[]),
            AIMessage(content="새 매물 3건 있습니다."),
        ]
    }
    assert _answer_of(state) == "새 매물 3건 있습니다."


def test_answer_of_handles_empty_state():
    assert _answer_of({}) == ""


# --- Outcome ---


def test_text_joins_answer_and_notice():
    assert Outcome("끝났습니다.", "\n\n(막힘)", ok=True).text == "끝났습니다.\n\n(막힘)"


# --- 실행 ---


class _Graph:
    """그래프 흉내. 확인이 걸리는 경우와 아닌 경우를 재현한다."""

    def __init__(self, interrupt_first: bool = False) -> None:
        self.interrupt_first = interrupt_first
        self.calls: list = []

    async def ainvoke(self, payload, config=None):
        self.calls.append(payload)
        if self.interrupt_first and len(self.calls) == 1:
            blocked = type("I", (), {"value": {"actions": [{"name": "send_email"}]}})()
            return {"__interrupt__": [blocked], "messages": []}
        return {"messages": [AIMessage(content="정리했습니다.")]}


@pytest.fixture
def graph(monkeypatch):
    """runtime.graph 를 갈아 끼운다. 원래 값은 monkeypatch 가 되돌린다."""
    from app.agent import runtime as runtime_mod

    def _install(g):
        monkeypatch.setattr(runtime_mod.runtime, "graph", g)
        return g

    return _install


async def test_plain_run_returns_the_answer(graph):
    g = graph(_Graph())
    result = await run_prompt("정리해줘", thread_prefix="t", timeout=5)

    assert result.ok
    assert result.answer == "정리했습니다."
    assert result.notice == ""


async def test_write_tool_is_cancelled_not_executed(graph):
    """승인해 줄 사람이 없으니 실행하면 안 된다. 대신 무엇이 막혔는지 알려야 한다."""
    g = graph(_Graph(interrupt_first=True))
    result = await run_prompt("메일 보내줘", thread_prefix="t", timeout=5)

    assert result.ok
    assert "send_email" in result.notice
    assert "실행하지 않았습니다" in result.notice
    # 확인을 만난 뒤 취소로 한 번 더 태워야 그래프가 정리된다
    assert len(g.calls) == 2


async def test_missing_graph_is_reported_not_raised(graph):
    """기동 직후엔 그래프가 없을 수 있다. 죽지 말고 사유를 돌려준다."""
    graph(None)
    result = await run_prompt("아무거나", thread_prefix="t", timeout=5)

    assert not result.ok
    assert "준비되지" in result.answer


async def test_timeout_is_reported_not_raised(graph):
    class _Slow:
        async def ainvoke(self, payload, config=None):
            import asyncio

            await asyncio.sleep(10)

    graph(_Slow())
    result = await run_prompt("오래 걸리는 일", thread_prefix="t", timeout=0.05)

    assert not result.ok
    assert "제한 시간" in result.answer


async def test_headless_run_cannot_delegate_again(graph):
    """맡겨 둔 작업 안에서 또 작업을 맡기면 아무도 일을 안 한다.

    실제로 그렇게 돌았다 — 백그라운드 워커가 start_background_task 를 다시 불렀고,
    쓰기 도구라 취소되면서 빈손으로 끝났다. 도구 목록에서 빼는 것으로 막는다.
    """
    from app.agent.nodes import _get_agent_llm

    seen = []

    class _Recorder:
        async def ainvoke(self, payload, config=None):
            seen.append(payload.get("headless"))
            return {"messages": [AIMessage(content="ok")]}

    graph(_Recorder())
    await run_prompt("조사해줘", thread_prefix="job", timeout=5)
    assert seen == [True], "헤드리스 실행이 스스로를 그렇게 표시해야 한다"


def test_headless_binding_drops_the_delegating_tool():
    from app.agent.nodes import _HEADLESS_EXCLUDED
    from app.tools import TOOLS

    names = {t.name for t in TOOLS}
    assert _HEADLESS_EXCLUDED <= names, "빼려는 도구 이름이 실제 도구와 어긋났다"


async def test_each_run_gets_its_own_thread(graph):
    """스레드가 겹치면 앞선 실행의 확인 대기에 다음 실행이 물린다."""
    seen = []

    class _Recorder:
        async def ainvoke(self, payload, config=None):
            seen.append(config["configurable"]["thread_id"])
            return {"messages": [AIMessage(content="ok")]}

    graph(_Recorder())
    await run_prompt("a", thread_prefix="job", timeout=5)
    await run_prompt("b", thread_prefix="job", timeout=5)

    assert len(set(seen)) == 2, seen
    assert all(t.startswith("job-") for t in seen)
