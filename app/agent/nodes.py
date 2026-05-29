import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agent.prompts import (
    PROFILE_UPDATE_PROMPT,
    REFLECT_PROMPT,
    build_system_prompt,
)
from app.agent.state import JarvisState
from app.db import audit
from app.llm import chat, fast
from app.memory.long_term import long_term
from app.memory.profile import load_summary, update_summary
from app.tools import TOOLS, TOOLS_BY_NAME, WRITE_TOOLS

log = logging.getLogger("javis.agent")


# 응답 경로 밖에서 도는 곁가지 작업(감사 로그·반추)용. 태스크 참조를 들고 있어야 GC 안 됨.
_bg_tasks: set[asyncio.Task] = set()

# 직전 턴 끝에서 읽어둔 감정을 스레드(대화)별로 보관한다. 반추가 응답 뒤 백그라운드로
# 채워 넣고, 다음 턴의 prepare 가 읽어 시스템 프롬프트에 싣는다. 감정은 한 턴 지연되지만
# 분류 LLM 호출이 임계 경로(첫 토큰까지)에서 사라진다. 프로세스 메모리라 재시작 시 중립부터.
_mood_by_thread: dict[str, str] = {}


def _thread_id(config: RunnableConfig | None) -> str:
    return ((config or {}).get("configurable") or {}).get("thread_id", "default")


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _last_human_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# --- 노드 ---


async def _load_summary_safe() -> str | None:
    try:
        return await load_summary()
    except Exception as exc:
        log.debug("프로필 로드 실패(무시): %s", exc)
        return None


async def _retrieve_safe(text: str) -> list[str]:
    try:
        return await long_term.retrieve(text, top_k=5)
    except Exception as exc:
        log.warning("기억 조회 실패: %s", exc)
        return []


async def prepare(state: JarvisState, config: RunnableConfig) -> dict:
    """응답 직전 맥락을 모은다: 프로필 요약 + 관련 기억 + 직전 턴 감정.

    프로필 로드(DB)와 기억 조회(임베딩+벡터)는 서로 독립이라 동시에 돌린다. 감정은
    분류 LLM 을 임계 경로에서 부르지 않고, 반추가 직전 턴 끝에 채워 둔 값을 읽어 쓴다.
    """
    profile = dict(state.get("user_profile") or {})
    text = _last_human_text(state["messages"])

    summary, context = await asyncio.gather(_load_summary_safe(), _retrieve_safe(text))
    if summary is not None:
        profile["summary"] = summary

    mood = _mood_by_thread.get(_thread_id(config), "중립")
    return {"user_profile": profile, "retrieved_context": context, "mood": mood}


# 도구 바인딩은 11개 스키마를 매번 OpenAI 포맷으로 변환한다. ReAct 루프가 여러 번
# 도는 걸 감안해 한 번만 묶어 재사용한다(온기를 살짝 주되 도구 신뢰성은 지키는 온도).
_agent_llm = None


def _get_agent_llm():
    global _agent_llm
    if _agent_llm is None:
        _agent_llm = chat(streaming=True, temperature=0.5).bind_tools(TOOLS)
    return _agent_llm


async def agent(state: JarvisState) -> dict:
    system = SystemMessage(content=build_system_prompt(state))
    response = await _get_agent_llm().ainvoke([system, *state["messages"]])
    return {"messages": [response]}


async def _run_tool_call(call: dict) -> ToolMessage:
    """도구 하나를 실행하고 감사로그까지 남긴 뒤 ToolMessage 로 돌려준다.

    동시 실행 중 하나가 터져 나머지를 말아먹지 않도록 예외는 여기서 삼킨다.
    감사 기록은 응답을 막을 이유가 없어 백그라운드로 떼어, 다음 agent 호출 전 DB 왕복을 없앤다.
    """
    tool = TOOLS_BY_NAME.get(call["name"])
    if tool is None:
        content = f"알 수 없는 도구: {call['name']}"
        _spawn(audit.record("tool", call["name"], call["args"], content, ok=False))
    else:
        try:
            content = await tool.ainvoke(call["args"])
            _spawn(audit.record("tool", call["name"], call["args"], content, ok=True))
        except Exception as exc:
            log.exception("도구 실행 오류: %s", call["name"])
            content = f"도구 실행 중 오류가 났습니다: {exc}"
            _spawn(audit.record("tool", call["name"], call["args"], str(exc), ok=False))
    return ToolMessage(content=str(content), tool_call_id=call["id"], name=call["name"])


async def execute_tools(state: JarvisState) -> dict:
    """도구 실행. 쓰기 도구가 포함되면 실행 전에 한 번 확인(interrupt)을 건다.

    interrupt 후 재개되면 이 노드는 처음부터 다시 실행된다. 그래서 확인은 맨 위에서
    배치 단위로 한 번만 걸어, 재실행 시 중복 호출이 생기지 않게 한다.
    """
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []

    write_calls = [c for c in calls if c["name"] in WRITE_TOOLS]
    if write_calls:
        approved = interrupt(
            {
                "type": "confirm",
                "message": "다음 작업을 실행할까요?",
                "actions": [{"name": c["name"], "args": c["args"]} for c in write_calls],
            }
        )
        if not approved:
            for c in write_calls:
                _spawn(audit.record("tool", c["name"], c["args"], "사용자 취소", ok=False))
            return {
                "messages": [
                    ToolMessage(
                        content="사용자가 작업을 취소했습니다.",
                        tool_call_id=c["id"],
                        name=c["name"],
                    )
                    for c in calls
                ]
            }

    # 한 턴에 여러 도구가 호출되면(서로 독립) 동시에 실행한다. 도구마다 별도 세션을
    # 쓰고 Google 호출은 to_thread 라 병렬이 안전하며, gather 가 호출 순서대로 결과를
    # 돌려줘 ToolMessage 순서(=tool_call 대응)는 보존된다.
    results = await asyncio.gather(*(_run_tool_call(c) for c in calls))
    return {"messages": list(results)}


class _MemoryFact(BaseModel):
    content: str
    category: str = "general"
    importance: int = 5


class _Reflection(BaseModel):
    items: list[_MemoryFact] = Field(default_factory=list)
    user_mood: str = Field(default="중립", description="대화 끝 시점 사용자의 감정을 한 구절로")


async def _update_profile(new_facts: list[_MemoryFact], old_summary: str | None) -> None:
    facts_text = "\n".join(f"- ({f.category}) {f.content}" for f in new_facts)
    message = f"[기존 프로필]\n{old_summary or '(없음)'}\n\n[새로 알게 된 사실]\n{facts_text}"
    updated = await fast().ainvoke(
        [SystemMessage(content=PROFILE_UPDATE_PROMPT), HumanMessage(content=message)]
    )
    text = updated.content if isinstance(updated.content, str) else str(updated.content)
    await update_summary(text)


async def _reflect_worker(messages: list, old_summary: str | None, thread_id: str) -> None:
    """기억 추출 + 프로필 갱신 + 다음 턴 감정 추출. 응답 경로 밖에서 도는 곁가지 작업.

    기억 추출과 감정 읽기를 한 번의 구조화 호출로 묶어, 백그라운드 LLM 왕복을 1회로 둔다.
    읽어낸 감정은 스레드별로 보관해 다음 턴 prepare 가 시스템 프롬프트에 싣는다.
    """
    try:
        transcript = "\n".join(
            f"{'사용자' if isinstance(m, HumanMessage) else '비서'}: {m.content}"
            for m in messages
            if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str) and m.content
        )
        if not transcript.strip():
            return

        extractor = fast().with_structured_output(_Reflection)
        result = await extractor.ainvoke(
            [SystemMessage(content=REFLECT_PROMPT), HumanMessage(content=transcript)]
        )
        _mood_by_thread[thread_id] = (result.user_mood or "중립").strip() or "중립"

        new_facts = [f for f in result.items if f.importance >= 4]
        if new_facts:
            await long_term.save_many([(f.content, f.category, f.importance) for f in new_facts])
            await _update_profile(new_facts, old_summary)
    except Exception as exc:
        log.warning("반추 단계 실패(무시): %s", exc)


async def reflect(state: JarvisState, config: RunnableConfig) -> dict:
    """기억 저장과 감정 읽기를 백그라운드로 떼어내 응답 지연을 없앤다.

    사용자는 기억 저장(LLM)을 기다릴 이유가 없으므로 태스크만 띄우고 그래프는 즉시
    종료한다. 여기서 읽은 감정은 다음 턴 응답에 반영된다(한 턴 지연).
    """
    recent = list(state["messages"][-6:])
    # prepare 가 이미 끌어온 프로필 요약을 넘겨, 프로필 갱신 때 같은 행을 다시 읽지 않게 한다.
    summary = (state.get("user_profile") or {}).get("summary")
    _spawn(_reflect_worker(recent, summary, _thread_id(config)))
    return {}
