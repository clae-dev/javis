import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agent.prompts import CLASSIFY_PROMPT, REFLECT_PROMPT, build_system_prompt
from app.agent.state import Intent, JarvisState
from app.db import audit
from app.llm import chat, fast
from app.memory.long_term import long_term
from app.tools import TOOLS, TOOLS_BY_NAME, WRITE_TOOLS

log = logging.getLogger("javis.agent")


def _last_human_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# --- 노드 ---


class _IntentResult(BaseModel):
    intent: Intent = Field(description="chat | query | task")


async def classify_intent(state: JarvisState) -> dict:
    text = _last_human_text(state["messages"])
    if not text:
        return {"intent": "chat"}
    classifier = fast().with_structured_output(_IntentResult)
    try:
        result = await classifier.ainvoke(
            [SystemMessage(content=CLASSIFY_PROMPT), HumanMessage(content=text)]
        )
        return {"intent": result.intent}
    except Exception as exc:  # 분류 실패 시 안전하게 task 로 (도구 접근 허용)
        log.warning("intent 분류 실패, task 로 폴백: %s", exc)
        return {"intent": "task"}


async def retrieve_memory(state: JarvisState) -> dict:
    text = _last_human_text(state["messages"])
    try:
        context = await long_term.retrieve(text, top_k=5)
    except Exception as exc:
        log.warning("기억 조회 실패: %s", exc)
        context = []
    return {"retrieved_context": context}


async def agent(state: JarvisState) -> dict:
    llm = chat(streaming=True).bind_tools(TOOLS)
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


async def reflect(state: JarvisState) -> dict:
    """대화 끝에서 기억할 가치가 있는 정보만 추려 장기 기억에 저장한다.

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
        for fact in extracted.items:
            if fact.importance >= 4:
                await long_term.save(fact.content, fact.category, fact.importance)
    except Exception as exc:
        log.warning("반추 단계 실패(무시): %s", exc)
    return {}
