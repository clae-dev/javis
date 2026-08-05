"""사람 없이 그래프를 한 번 태우는 통로.

스케줄러가 걸어 둔 정기 작업과, 사용자가 시켜 놓고 자리를 뜬 백그라운드 작업이
같은 처지다 — 승인해 줄 사람이 없는 자리에서 도는데, 쓰기 도구를 만나면 그래프가
확인을 요구하며 멈춘다. 그래서 양쪽 다 같은 처리를 한다: 확인이 걸리면 실행하지 않고
취소로 정리한 뒤, 무엇이 막혔는지를 답변에 실어 돌려준다.

두 곳이 각자 구현하면 한쪽만 고쳐 놓고 다른 쪽은 잊기 좋은 종류의 코드라 한곳에 둔다.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings

log = logging.getLogger("javis.headless")


@dataclass(frozen=True)
class Outcome:
    """헤드리스 실행 결과."""

    answer: str
    #  확인이 필요해 실행하지 않은 작업이 있으면 그 사유. 없으면 빈 문자열.
    notice: str
    ok: bool

    @property
    def text(self) -> str:
        """사람에게 그대로 보여줄 한 덩어리."""
        return f"{self.answer}{self.notice}"


def _answer_of(state: dict) -> str:
    """그래프 최종 상태에서 마지막 답변 텍스트만 뽑는다."""
    from langchain_core.messages import AIMessage

    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and isinstance(message.content, str) and message.content.strip():
            return message.content.strip()
    return ""


async def run_prompt(prompt: str, *, thread_prefix: str, timeout: float) -> Outcome:
    """프롬프트 하나를 사람 없이 그래프에 태우고 답을 받는다.

    실행마다 대화 맥락을 새로 시작한다. 이런 작업은 이어 말하기가 아니라 매번 독립이고,
    앞선 실행에 확인 대기가 남아 있어도 다음 실행이 거기 물리지 않는다.
    """
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from app.agent.runtime import runtime

    if runtime.graph is None:
        return Outcome("그래프가 아직 준비되지 않았습니다.", "", ok=False)

    thread = f"{thread_prefix}-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
    config = {"configurable": {"thread_id": thread}}
    payload = {
        "messages": [HumanMessage(content=prompt)],
        "user_profile": {"name": settings.owner_name},
        # 일을 또 남에게 미루지 못하게 도구 목록을 좁힌다. 여기서 도는 게 이미 그 '남'이다.
        "headless": True,
    }

    notice = ""
    try:
        state = await asyncio.wait_for(runtime.graph.ainvoke(payload, config=config), timeout=timeout)
        if "__interrupt__" in state:
            blocked = state["__interrupt__"][0].value or {}
            names = ", ".join(a.get("name", "?") for a in blocked.get("actions", []))
            notice = f"\n\n(확인이 필요한 작업이라 실행하지 않았습니다: {names})"
            state = await asyncio.wait_for(
                runtime.graph.ainvoke(Command(resume=False), config=config), timeout=timeout
            )
        return Outcome(_answer_of(state) or "(답변이 비었습니다)", notice, ok=True)
    except asyncio.TimeoutError:
        return Outcome("작업이 제한 시간 안에 끝나지 않았습니다.", "", ok=False)
    except Exception as exc:
        log.exception("헤드리스 실행 실패: %s", thread_prefix)
        return Outcome(f"작업 중 오류가 났습니다: {exc}", "", ok=False)
