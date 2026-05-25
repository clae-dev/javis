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


async def analyze(state: JarvisState) -> dict:
    """의도와 감정을 한 번에 읽고, 사용자 프로필을 끌어와 상태에 싣는다."""
    profile = dict(state.get("user_profile") or {})
    try:
        profile["summary"] = await load_summary()
    except Exception as exc:
        log.debug("프로필 로드 실패(무시): %s", exc)

    text = _last_human_text(state["messages"])
    if not text:
        return {"intent": "chat", "mood": "중립", "user_profile": profile}

    analyzer = fast().with_structured_output(_Analysis)
    try:
        result = await analyzer.ainvoke(
            [SystemMessage(content=ANALYZE_PROMPT), HumanMessage(content=text)]
        )
        return {"intent": result.intent, "mood": result.user_mood, "user_profile": profile}
    except Exception as exc:  # 분석 실패 시 안전하게 task 로 (도구 접근 허용)
        log.warning("분석 실패, task 로 폴백: %s", exc)
        return {"intent": "task", "mood": "중립", "user_profile": profile}


async def retrieve_memory(state: JarvisState) -> dict:
    text = _last_human_text(state["messages"])
    try:
        context = await long_term.retrieve(text, top_k=5)
    except Exception as exc:
        log.warning("기억 조회 실패: %s", exc)
        context = []
    return {"retrieved_context": context}


async def agent(state: JarvisState) -> dict:
    # 온기를 살짝 주되 도구 호출 신뢰성은 유지되는 선의 온도.
    llm = chat(streaming=True, temperature=0.5).bind_tools(TOOLS)
    system = SystemMessage(content=build_system_prompt(state))
    response = await llm.ainvoke([system, *state["messages"]])
    return {"messages": [response]}


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

    results: list[ToolMessage] = []
    for call in calls:
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
        results.append(
            ToolMessage(content=str(content), tool_call_id=call["id"], name=call["name"])
        )
    return {"messages": results}


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


async def reflect(state: JarvisState) -> dict:
    """대화 끝에서 기억할 가치가 있는 정보를 추려 장기 기억에 저장하고, 프로필을 갱신한다.

    어떤 이유로든 실패해도 사용자 응답에는 영향이 없도록 전부 삼킨다.
    """
    try:
        recent = state["messages"][-6:]
        transcript = "\n".join(
            f"{'사용자' if isinstance(m, HumanMessage) else '비서'}: {m.content}"
            for m in recent
            if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str) and m.content
        )
        if not transcript.strip():
            return {}

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
    return {}
