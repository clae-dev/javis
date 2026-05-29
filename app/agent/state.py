from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class JarvisState(TypedDict):
    # 대화 히스토리. add_messages 가 누적/병합을 알아서 처리한다.
    messages: Annotated[list[AnyMessage], add_messages]

    # 직전 턴 끝에서 반추가 읽어낸 사용자의 감정 상태 (한 구절). 공감 반응의 근거.
    mood: Optional[str]

    # 장기 기억에서 끌어온 컨텍스트.
    retrieved_context: list[str]

    # 사용자 정적 정보 (이름) + 누적 프로필 요약(summary). 시스템 프롬프트에 주입.
    user_profile: dict
