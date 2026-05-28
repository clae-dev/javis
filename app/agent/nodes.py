import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agent.prompts import (
    ANALYZE_PROMPT,
    PROFILE_UPDATE_PROMPT,
    REFLECT_PROMPT,
    build_system_prompt,
)
from app.agent.state import Intent, JarvisState
from app.db import audit
from app.llm import chat, fast
from app.memory.long_term import long_term
from app.memory.profile import load_summary, update_summary
from app.tools import TOOLS, TOOLS_BY_NAME, WRITE_TOOLS

log = logging.getLogger("javis.agent")


def _last_human_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# --- 노드 ---


class _Analysis(BaseModel):
    intent: Intent = Field(description="chat | query | task")
    user_mood: str = Field(default="중립", description="사용자의 현재 감정을 한 구절로")


async def _load_summary_safe() -> str | None:
    try:
        return await load_summary()
    except Exception as exc:
        log.debug("프로필 로드 실패(무시): %s", exc)
        return None


async def _classify(text: str) -> _Analysis | None:
    if not text:
        return None
    analyzer = fast().with_structured_output(_Analysis)
    try:
        return await analyzer.ainvoke(
            [SystemMessage(content=ANALYZE_PROMPT), HumanMessage(content=text)]
        )
    except Exception as exc:  # 분석 실패 시 안전하게 task 로 (도구 접근 허용)
        log.warning("분석 실패, task 로 폴백: %s", exc)
        return _Analysis(intent="task", user_mood="중립")


async def analyze(state: JarvisState) -> dict:
    """의도와 감정을 한 번에 읽고, 사용자 프로필을 끌어와 상태에 싣는다.

    프로필 로드(DB)와 의도/감정 분류(LLM)는 서로 독립이라 동시에 돌려, 매 메시지의
    임계 경로에서 DB 왕복 한 번을 덜어낸다.
    """
    profile = dict(state.get("user_profile") or {})
    text = _last_human_text(state["messages"])

    summary, result = await asyncio.gather(_load_summary_safe(), _classify(text))
    if summary is not None:
        profile["summary"] = summary
    if result is None:  # 사용자 발화가 없으면 잡담으로 둔다.
        return {"intent": "chat", "mood": "중립", "user_profile": profile}
    return {"intent": result.intent, "mood": result.user_mood, "user_profile": profile}


async def retrieve_memory(state: JarvisState) -> dict:
    text = _last_human_text(state["messages"])
    try:
        context = await long_term.retrieve(text, top_k=5)
    except Exception as exc:
        log.warning("기억 조회 실패: %s", exc)
        context = []
    return {"retrieved_context": context}


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
    """
    tool = TOOLS_BY_NAME.get(call["name"])
    if tool is None:
        content = f"알 수 없는 도구: {call['name']}"
        await audit.record("tool", call["name"], call["args"], content, ok=False)
    else:
        try:
            content = await tool.ainvoke(call["args"])
            await audit.record("tool", call["name"], call["args"], content, ok=True)
        except Exception as exc:
            log.exception("도구 실행 오류: %s", call["name"])
            content = f"도구 실행 중 오류가 났습니다: {exc}"
            await audit.record("tool", call["name"], call["args"], str(exc), ok=False)
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
                await audit.record("tool", c["name"], c["args"], "사용자 취소", ok=False)
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


class _MemoryExtract(BaseModel):
    items: list[_MemoryFact] = Field(default_factory=list)


async def _update_profile(new_facts: list[_MemoryFact]) -> None:
    old = await load_summary()
    facts_text = "\n".join(f"- ({f.category}) {f.content}" for f in new_facts)
    message = f"[기존 프로필]\n{old or '(없음)'}\n\n[새로 알게 된 사실]\n{facts_text}"
    updated = await fast().ainvoke(
        [SystemMessage(content=PROFILE_UPDATE_PROMPT), HumanMessage(content=message)]
    )
    text = updated.content if isinstance(updated.content, str) else str(updated.content)
    await update_summary(text)


# 백그라운드 반추 태스크 참조 보관(GC 방지).
_bg_tasks: set[asyncio.Task] = set()


async def _reflect_worker(messages: list) -> None:
    """기억 추출 + 프로필 갱신. 응답 경로 밖에서 도는 곁가지 작업."""
    try:
        transcript = "\n".join(
            f"{'사용자' if isinstance(m, HumanMessage) else '비서'}: {m.content}"
            for m in messages
            if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str) and m.content
        )
        if not transcript.strip():
            return

        extractor = fast().with_structured_output(_MemoryExtract)
        extracted = await extractor.ainvoke(
            [SystemMessage(content=REFLECT_PROMPT), HumanMessage(content=transcript)]
        )
        new_facts = [f for f in extracted.items if f.importance >= 4]
        for fact in new_facts:
            await long_term.save(fact.content, fact.category, fact.importance)
        if new_facts:
            await _update_profile(new_facts)
    except Exception as exc:
        log.warning("반추 단계 실패(무시): %s", exc)


async def reflect(state: JarvisState) -> dict:
    """기억 저장을 백그라운드로 떼어내 응답 지연을 없앤다.

    사용자는 기억 저장(LLM 1~2회)을 기다릴 이유가 없으므로, 태스크만 띄우고
    그래프는 즉시 종료한다. 실제 저장은 응답이 나간 뒤 이어서 끝난다.
    """
    recent = list(state["messages"][-6:])
    task = asyncio.create_task(_reflect_worker(recent))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {}
