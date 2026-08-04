"""안드로이드 도구 — dumpsys 파싱, 앱 이름 해석, 문자 명령 조립.

폰 없이 검증할 수 있는 부분만 본다. adb 호출은 _shell 을 갈아끼워 가로챈다.
"""

import shlex

import pytest

from app.tools import android

DUMPSYS = """
Current Notification List:
  NotificationRecord(0x1: pkg=com.kakao.talk user=UserHandle{0} id=1 key=0|com.kakao.talk|1|null|10123
    extras={
      android.title=String (김철수)
      android.text=String (내일 몇시에 봐요?)
    }
  NotificationRecord(0x2: pkg=com.samsung.android.messaging user=UserHandle{0} id=2
    extras={
      android.title=String (010-1234-5678)
      android.text=SpannableString (인증번호 [123456])
    }
  NotificationRecord(0x3: pkg=com.android.systemui user=UserHandle{0} id=3
    extras={
      android.title=null
      android.text=null
    }
"""


@pytest.fixture
def shell(monkeypatch):
    """_shell 로 나간 명령을 모으고, 미리 정한 출력을 돌려준다."""
    calls: list[str] = []
    state = {"out": "", "err": None}

    async def fake_shell(cmd, timeout=None):
        calls.append(cmd)
        return state["out"], state["err"]

    monkeypatch.setattr(android, "_shell", fake_shell)
    return calls, state


# --- 알림 파싱 ---


async def test_notifications_are_parsed(shell):
    calls, state = shell
    state["out"] = DUMPSYS

    items = await android.list_android_notifications.ainvoke({})
    assert items == [
        {"app": "com.kakao.talk", "title": "김철수", "text": "내일 몇시에 봐요?"},
        {"app": "com.samsung.android.messaging", "title": "010-1234-5678", "text": "인증번호 [123456]"},
    ]


async def test_null_only_records_are_dropped(shell):
    """제목·본문이 다 null 인 시스템 알림은 읽어 봐야 소용없다."""
    calls, state = shell
    state["out"] = DUMPSYS
    items = await android.list_android_notifications.ainvoke({})
    assert all(i["app"] != "com.android.systemui" for i in items)


async def test_notifications_respect_limit(shell):
    calls, state = shell
    state["out"] = DUMPSYS
    items = await android.list_android_notifications.ainvoke({"limit": 1})
    assert len(items) == 1


async def test_empty_dump_reports_nothing(shell):
    calls, state = shell
    state["out"] = "Current Notification List:\n"
    assert "없습니다" in await android.list_android_notifications.ainvoke({})


# --- 앱 해석 ---


async def test_known_alias_skips_package_listing(shell):
    calls, state = shell
    package, err = await android._resolve_package("카톡")
    assert (package, err) == ("com.kakao.talk", None)
    assert calls == []  # 별칭으로 끝났으면 폰에 물어볼 이유가 없다


async def test_package_name_passes_through(shell):
    package, err = await android._resolve_package("com.example.app")
    assert (package, err) == ("com.example.app", None)


async def test_unknown_app_is_searched_on_device(shell):
    calls, state = shell
    state["out"] = "package:com.example.notes\npackage:com.example.notes.widget\n"
    package, err = await android._resolve_package("notes")
    # 접미사가 붙은 부가 패키지가 아니라 본체를 고른다.
    assert package == "com.example.notes"
    assert calls == ["pm list packages"]


async def test_missing_app_reports_clearly(shell):
    calls, state = shell
    state["out"] = "package:com.example.other\n"
    package, err = await android._resolve_package("스포티파이없음")
    assert package is None and "찾지 못했" in err


async def test_url_opens_with_view_intent(shell):
    calls, state = shell
    await android.open_android_app.ainvoke({"target": "https://map.naver.com/route"})
    assert "android.intent.action.VIEW" in calls[0]
    assert "https://map.naver.com/route" in calls[0]


# --- 미디어 ---


async def test_media_maps_to_keyevent(shell):
    calls, state = shell
    await android.android_media.ainvoke({"action": "next"})
    assert calls == ["input keyevent KEYCODE_MEDIA_NEXT"]


async def test_unknown_media_action_lists_options(shell):
    calls, state = shell
    result = await android.android_media.ainvoke({"action": "춤춰"})
    assert calls == []
    assert "play_pause" in result


# --- 문자 ---


async def test_sms_composes_without_sending_by_default(shell, monkeypatch):
    calls, state = shell
    monkeypatch.setattr(android.settings, "android_sms_autosend", False)

    result = await android.send_sms.ainvoke({"to": "010-1234-5678", "body": "지금 출발해"})
    assert len(calls) == 1  # 작성만 하고 키 입력은 없다
    assert "smsto:01012345678" in calls[0]
    assert "눌러 주세요" in result


async def test_sms_body_survives_shell_quoting(shell):
    """따옴표·세미콜론이 든 내용이 폰 쪽 셸에서 쪼개지면 안 된다.

    adb 는 인자를 이어 붙여 기기 셸에 넘기므로, 여기서 인용하지 않으면
    ';' 뒤가 별도 명령으로 실행된다.
    """
    calls, state = shell
    body = "오늘 '회의' 취소됐어; 쉬어"
    await android.send_sms.ainvoke({"to": "01011112222", "body": body})

    parts = shlex.split(calls[0])
    assert parts[parts.index("--es") + 1] == "sms_body"
    assert parts[parts.index("--es") + 2] == body
    assert "--ez exit_on_sent true" in calls[0]


async def test_sms_rejects_unparseable_number(shell):
    calls, state = shell
    result = await android.send_sms.ainvoke({"to": "없음", "body": "안녕"})
    assert calls == []
    assert "전화번호" in result


async def test_sms_rejects_empty_body(shell):
    calls, state = shell
    result = await android.send_sms.ainvoke({"to": "01011112222", "body": "   "})
    assert calls == []
    assert "비어" in result


async def test_setup_hint_without_adb(monkeypatch):
    monkeypatch.setattr(android.settings, "android_adb_path", "")
    monkeypatch.setattr(android.shutil, "which", lambda _: None)
    out, err = await android._adb(["devices"])
    assert err == android.SETUP_HINT
