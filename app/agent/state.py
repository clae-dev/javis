from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Intent = Literal["chat", "task", "query"]


class JarvisState(TypedDict):
    # 대화 히스토리. add_messages 가 누적/병합을 알아서 처리한다.
    messages: Annotated[list[AnyMessage], add_messages]

    # 이번 턴의 의도. chat=잡담, query=조회성 질문, task=실행이 필요한 작업.
    intent: Optional[Intent]

    # 장기 기억에서 끌어온 컨텍스트.
    retrieved_context: list[str]

    # 사용자 정적 정보 (이름 등). WS 진입 시 주입.
    user_profile: dict
