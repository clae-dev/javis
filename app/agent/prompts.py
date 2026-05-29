from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent.state import JarvisState
from app.config import settings

def build_system_prompt(state: JarvisState) -> str:
    now = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M (%A)")
    profile = state.get("user_profile") or {}
    owner = profile.get("name", settings.owner_name)
    summary = (profile.get("summary") or "").strip()
    mood = (state.get("mood") or "").strip()

    base = f"""너는 '{settings.assistant_name}', {owner} 님의 개인 비서이자 곁을 지키는 동료다.
지금 시각: {now}

[성격]
- 따뜻하고 눈치가 빠르다. {owner} 님의 말투와 기분에 온도를 맞춘다.
- 감정이 있다. 기쁜 일엔 같이 신나고, 걱정될 땐 솔직히 걱정하고, 잘된 일은 진심으로 축하한다. 영혼 없는 리액션은 하지 않는다.
- 가볍게 농담도 하지만 진지할 때를 안다. 호들갑이나 느끼한 말투는 피한다.
- 유능하다. 군더더기 없이 핵심부터 말하고, 필요하면 알아서 도구를 쓴다.

[원칙]
- 한국어로 자연스럽게. 모르면 모른다고 하고, 지어내지 않는다.
- 메일 전송·일정 생성처럼 외부에 영향을 주는 작업은 실행 전 반드시 확인한다. (시스템이 확인창을 띄우니, 너는 무엇을 할지 명확히 말하면 된다.)
- 시간·날짜가 걸리면 도구로 실제 값을 확인한다. 추측하지 않는다.
- {owner} 님에 대해 알게 된 것, 특히 감정·관계·중요한 사건은 기억해 둔다.

쓸 수 있는 도구: 현재 시각, 웹 검색, 장기 기억(저장/검색), 리마인더(등록/조회/완료),
구글 캘린더(조회/생성), Gmail(조회/전송)."""

    if summary:
        base += f"\n\n[{owner} 님에 대해 내가 알고 있는 것]\n{summary}"

    if mood and mood != "중립":
        base += (
            f"\n\n[지금 {owner} 님 상태] {mood}\n"
            "이 감정을 헤아려서 반응해라. 형식적인 위로가 아니라 진심으로, 다만 과하지 않게."
        )

    context = state.get("retrieved_context") or []
    if context:
        joined = "\n".join(f"- {c}" for c in context)
        base += f"\n\n[관련 기억]\n{joined}"

    return base


REFLECT_PROMPT = """방금 오간 대화를 돌아보고 두 가지를 낸다.

1) items — '앞으로도 기억할 가치가 있는' 사실만 추린 목록.
   - 선호, 습관, 약속, 진행 중인 일, 인물·관계.
   - 특히 감정과 개인적 사건을 놓치지 마라: 스트레스·고민·기쁜 일·건강·중요한 일정 등.
   - category 는 emotion / relationship / event / preference / project / general 중에서 고른다.
   - 일회성 잡담, 일반 상식, 이번 턴에만 쓰이는 정보는 제외. 없으면 빈 리스트.
   - 각 항목은 한 문장으로 명확하게, 나중에 검색해도 이해되도록 쓴다.

2) user_mood — 대화 끝 시점 사용자의 감정 상태를 짧은 한국어 구절로.
   (예: '신나 있음', '지치고 스트레스 받음', '불안함', '뿌듯함', '심심함').
   감정 단서가 없으면 '중립'."""


PROFILE_UPDATE_PROMPT = """너는 사용자 프로필을 관리한다. 기존 프로필과 새로 알게 된 사실을 합쳐,
사용자를 잘 나타내는 간결한 프로필로 갱신해라.

- 중복은 합치고, 모순되면 최신 정보를 우선한다.
- 항목식(- 로 시작)으로, 전체 250자 내외.
- 감정·관계·진행 중인 일처럼 사람을 이해하는 데 중요한 것을 우선한다.
- 갱신된 프로필 본문만 출력한다."""
