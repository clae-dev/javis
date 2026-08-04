"""눈 — 카메라로 보고 알아본다.

프레임을 상시 올려두지 않는다. 물어봤을 때만 데몬에게 한 장 달라고 해서 그때 한 번
vision 모델에 보낸다. 상시 전송은 비용·지연·프라이버시 전부 손해라서다.

누가 있는지는 다르다. 매칭이 데몬에서 로컬로 끝나 있어, 서버는 그 결과만 받아 두고
질문이 오면 바로 답한다 — 이쪽은 클라우드 왕복이 아예 없다.
"""

import base64
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.api import vision as channel
from app.config import settings
from app.llm import chat

LOOK_PROMPT = """카메라로 찍은 사진을 보고 묻는 말에 답한다.

- 보이는 것만 말한다. 흐릿하거나 잘려서 확신이 없으면 그렇다고 말한다.
- 사람이 있으면 무엇을 하고 있는지 정도만 말하고, 외모를 품평하거나 신원을 추측하지 않는다.
- 음성으로 읽힐 수 있으니 세 문장 안쪽으로 짧게 답한다."""


@tool
async def look(question: str = "") -> str:
    """카메라로 지금 무엇이 보이는지 확인한다.

    "이거 뭐야", "지금 뭐 보여", "이 물건 이름이 뭐지" 처럼 눈으로 봐야 답할 수 있는
    질문에 쓴다. 누가 있는지만 궁금하면 who_is_here 가 더 빠르고 정확하다.

    Args:
        question: 사진에 대해 물어볼 내용. 비우면 보이는 것을 그대로 설명한다.
    """
    if not settings.has_openai:
        return "OPENAI_API_KEY 가 없어 사진을 볼 수 없습니다."

    frame, err = await channel.capture()
    if err:
        return err

    encoded = base64.b64encode(frame).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": question.strip() or "지금 보이는 것을 설명해줘."},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
        ]
    )

    model = chat(settings.vision_model or None, temperature=0.2)
    try:
        reply = await model.ainvoke([SystemMessage(content=LOOK_PROMPT), message])
    except Exception as exc:
        return f"사진을 보는 중 문제가 있었습니다: {exc}"

    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    return text.strip() or "무엇인지 알아보지 못했습니다."


@tool
async def who_is_here() -> str:
    """카메라에 지금 누가 보이는지 확인한다. 등록해 둔 얼굴만 이름으로 알아본다."""
    if not channel.connected():
        return channel.SETUP_HINT

    seen = channel.latest_sighting()
    if seen is None:
        return "지금은 카메라에 아무도 보이지 않습니다."

    ago = int((datetime.now(timezone.utc) - seen.at).total_seconds())
    when = "방금" if ago < 5 else f"{ago}초 전"

    if seen.names and seen.unknown:
        who = f"{', '.join(seen.names)} 님, 그리고 모르는 사람 {seen.unknown}명"
    elif seen.names:
        who = f"{', '.join(seen.names)} 님"
    elif seen.unknown:
        who = f"등록되지 않은 사람 {seen.unknown}명"
    else:
        return "지금은 카메라에 아무도 보이지 않습니다."

    return f"{when} 기준으로 {who}이(가) 보입니다."
