"""도구 레지스트리.

새 도구를 추가할 때는 모듈에서 `@tool` 함수를 만들고 여기 리스트에 넣으면 된다.
외부에 영향을 주는(쓰기) 도구는 WRITE_TOOLS 에도 이름을 넣어야 실행 전 확인을 거친다.
"""

from app.tools.android import (
    android_media,
    android_screenshot,
    list_android_notifications,
    open_android_app,
    place_call,
    send_sms,
)
from app.tools.browser import browse, browse_extract, browser_act
from app.tools.builtin import get_current_time, recall, remember
from app.tools.calendar import create_event, get_upcoming_events
from app.tools.code import ask_project, edit_project
from app.tools.gmail import list_recent_emails, send_email
from app.tools.hue import get_hue_lights, set_hue_light
from app.tools.maps import get_directions, search_place
from app.tools.notes import (
    append_note,
    narrate_notes,
    read_note,
    reindex_notes,
    search_notes,
)
from app.tools.reminders import complete_reminder, create_reminder, list_reminders
from app.tools.schedules import create_schedule, delete_schedule, list_schedules
from app.tools.search import web_search
from app.tools.tasks import list_background_tasks, start_background_task
from app.tools.vision import look, who_is_here
from app.tools.youtube import get_video_stats, list_channel_videos, search_youtube

TOOLS = [
    get_current_time,
    web_search,
    remember,
    recall,
    search_notes,
    read_note,
    append_note,
    reindex_notes,
    narrate_notes,
    create_reminder,
    list_reminders,
    complete_reminder,
    create_schedule,
    list_schedules,
    delete_schedule,
    start_background_task,
    list_background_tasks,
    get_upcoming_events,
    create_event,
    list_recent_emails,
    send_email,
    ask_project,
    edit_project,
    get_hue_lights,
    set_hue_light,
    list_android_notifications,
    open_android_app,
    android_media,
    android_screenshot,
    send_sms,
    place_call,
    browse,
    browse_extract,
    browser_act,
    search_youtube,
    get_video_stats,
    list_channel_videos,
    look,
    who_is_here,
    search_place,
    get_directions,
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}

# 실행 전 사용자 확인이 필요한 도구 (외부에 영향을 주는 것)
WRITE_TOOLS = {
    "create_event",
    "send_email",
    "edit_project",
    "set_hue_light",
    "send_sms",
    "place_call",
    "append_note",
    "browser_act",
    # 한 번 걸면 계속 도는 규칙이라, 만들고 지울 때 확인을 거친다.
    "create_schedule",
    "delete_schedule",
    # 맡긴 일은 몇 분씩 실제 비용을 쓰며 돈다. 안에서 쓰기 도구는 어차피 막히지만,
    # 시작 자체를 한 번 묻는다. 매번 묻는 게 성가시면 이 줄만 빼면 된다.
    "start_background_task",
}
