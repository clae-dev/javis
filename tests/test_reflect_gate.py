"""반추를 돌릴 턴인지 고르는 문지기.

반추는 응답 뒤 백그라운드로 도니까 지연은 없지만, 턴마다 LLM 왕복이 붙는다.
"고마워" 한 마디에까지 붙으면 쓸모 없는 비용이 계속 쌓인다. 반대로 문지기가
너무 엄하면 정작 기억해야 할 얘기를 흘린다 — 양쪽을 여기서 못박아 둔다.
"""

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.nodes import _worth_reflecting


def _ai_with_tool(name: str = "search_notes") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": "call_1"}],
    )


# --- 건너뛰는 턴 ---


def test_short_acknowledgement_is_skipped():
    turn = [HumanMessage(content="고마워"), AIMessage(content="네, 도움이 됐다니 다행이에요.")]
    assert not _worth_reflecting(turn)


def test_single_word_is_skipped():
    assert not _worth_reflecting([HumanMessage(content="응"), AIMessage(content="네.")])


# --- 돌리는 턴 ---


def test_substantial_utterance_is_reflected():
    turn = [
        HumanMessage(content="다음 주에 제주 가는데 숙소를 아직 못 잡았어"),
        AIMessage(content="언제 출발하세요?"),
    ]
    assert _worth_reflecting(turn)


def test_short_utterance_with_a_tool_call_is_reflected():
    """짧아도 도구를 썼으면 실제로 뭔가 한 턴이다."""
    turn = [HumanMessage(content="불 꺼줘"), _ai_with_tool("set_light")]
    assert _worth_reflecting(turn)


# --- 턴 경계 ---


def test_earlier_turns_do_not_leak_in():
    """앞 턴에서 도구를 썼다고 이번 맞장구까지 반추가 붙으면 안 된다.

    상태에 남은 최근 6개를 넘겨받기 때문에, 직전 사용자 발화 이후만 봐야 한다.
    """
    history = [
        HumanMessage(content="거실 불 좀 꺼줘"),
        _ai_with_tool("set_light"),
        AIMessage(content="껐어요."),
        HumanMessage(content="고마워"),
        AIMessage(content="네."),
    ]
    assert not _worth_reflecting(history)


def test_current_turn_is_judged_on_its_own():
    """반대로 앞 턴이 짧았어도 이번 턴이 알맹이가 있으면 돌려야 한다."""
    history = [
        HumanMessage(content="응"),
        AIMessage(content="네."),
        HumanMessage(content="요즘 회사 일이 너무 많아서 계속 지쳐 있어"),
        AIMessage(content="많이 힘드시겠어요."),
    ]
    assert _worth_reflecting(history)
