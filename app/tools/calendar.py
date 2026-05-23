from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from app.tools._google import SETUP_HINT, build_service


@tool
async def get_upcoming_events(days: int = 7) -> list[dict] | str:
    """앞으로 N일 이내의 캘린더 일정을 조회한다.

    Args:
        days: 조회할 일수 (기본 7, 최대 30).

    Returns:
        [{summary, start, end, location}, ...]
    """
    service = build_service("calendar", "v3")
    if service is None:
        return SETUP_HINT

    days = max(1, min(days, 30))
    now = datetime.now(timezone.utc)
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return [
        {
            "summary": e.get("summary", "(제목 없음)"),
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "end": e["end"].get("dateTime", e["end"].get("date")),
            "location": e.get("location", ""),
        }
        for e in result.get("items", [])
    ]


@tool
async def create_event(
    summary: str,
    start_iso: str,
    end_iso: str,
    location: str = "",
    description: str = "",
) -> dict | str:
    """캘린더에 새 일정을 만든다. 외부에 영향을 주므로 실행 전 사용자 확인을 거친다.

    Args:
        summary: 일정 제목.
        start_iso: 시작 시각 (ISO 8601, 예: 2026-05-25T14:00:00+09:00).
        end_iso: 종료 시각 (ISO 8601).
        location: 장소 (선택).
        description: 설명 (선택).
    """
    service = build_service("calendar", "v3")
    if service is None:
        return SETUP_HINT

    body = {
        "summary": summary,
        "location": location,
        "description": description,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    created = service.events().insert(calendarId="primary", body=body).execute()
    return {
        "id": created.get("id"),
        "summary": created.get("summary"),
        "htmlLink": created.get("htmlLink"),
    }
