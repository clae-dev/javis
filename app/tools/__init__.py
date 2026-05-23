"""도구 레지스트리.

새 도구를 추가할 때는 모듈에서 `@tool` 함수를 만들고 여기 리스트에 넣으면 된다.
외부에 영향을 주는(쓰기) 도구는 WRITE_TOOLS 에도 이름을 넣어야 실행 전 확인을 거친다.
"""

from app.tools.builtin import get_current_time, recall, remember
from app.tools.calendar import create_event, get_upcoming_events
from app.tools.gmail import list_recent_emails, send_email
from app.tools.reminders import complete_reminder, create_reminder, list_reminders
from app.tools.search import web_search

TOOLS = [
    get_current_time,
    web_search,
    remember,
    recall,
    create_reminder,
    list_reminders,
    complete_reminder,
    get_upcoming_events,
    create_event,
    list_recent_emails,
    send_email,
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}

# 실행 전 사용자 확인이 필요한 도구 (외부에 영향을 주는 것)
WRITE_TOOLS = {
    "create_event",
    "send_email",
}
