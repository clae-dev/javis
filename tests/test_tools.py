"""도구 레지스트리 정합성.

도구는 앞으로 계속 늘어난다. 이름이 겹치거나, 쓰기 도구를 WRITE_TOOLS 에 안 넣어
확인 절차를 건너뛰는 실수를 여기서 잡는다.
"""

from app.tools import TOOLS, TOOLS_BY_NAME, WRITE_TOOLS


def test_names_are_unique():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names)), f"이름이 겹치는 도구가 있다: {names}"


def test_lookup_covers_every_tool():
    assert set(TOOLS_BY_NAME) == {t.name for t in TOOLS}


def test_write_tools_exist():
    unknown = WRITE_TOOLS - set(TOOLS_BY_NAME)
    assert not unknown, f"등록되지 않은 도구가 WRITE_TOOLS 에 있다: {unknown}"


def test_every_tool_has_a_description():
    """설명이 없으면 모델이 언제 부를지 판단할 수 없다."""
    missing = [t.name for t in TOOLS if not (t.description or "").strip()]
    assert not missing, f"설명이 비어 있는 도구: {missing}"
