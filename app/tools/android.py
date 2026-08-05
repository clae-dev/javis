"""안드로이드폰 제어 (ADB).

폰을 USB 로 꽂거나 무선 디버깅(`adb connect <폰IP>:5555`)으로 붙여 두면, 알림을 읽고
앱을 띄우고 음악을 넘기고 문자를 쓸 수 있다.

문자는 기본적으로 '작성까지만' 한다. 자동 전송은 메시지 앱 화면 구성에 기대는 방식이라
기기에 따라 엉뚱한 버튼을 누를 수 있어서, ANDROID_SMS_AUTOSEND 로 따로 켜야 동작한다.
전화도 같은 태도다 — 기본은 다이얼러에 번호만 채우고, 통화 버튼은 사람이 누른다.
"""

import asyncio
import logging
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path

from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger("javis.android")

SETUP_HINT = (
    "안드로이드 연결이 안 되어 있습니다. 폰의 개발자 옵션에서 USB 디버깅을 켜고 "
    "케이블로 연결하거나, 무선 디버깅을 켠 뒤 `adb connect <폰IP>:5555` 를 한 번 "
    "실행해 주세요. (adb 가 PATH 에 없으면 .env 의 ANDROID_ADB_PATH 에 경로를 적습니다.)"
)

_TIMEOUT = 20.0

# 말로 부르는 이름 → 패키지. 여기 없는 앱은 `pm list packages` 에서 이름으로 찾는다.
_APP_ALIASES = {
    "카카오톡": "com.kakao.talk", "카톡": "com.kakao.talk", "kakaotalk": "com.kakao.talk",
    "유튜브": "com.google.android.youtube", "youtube": "com.google.android.youtube",
    "네이버": "com.nhn.android.search", "naver": "com.nhn.android.search",
    "네이버지도": "com.nhn.android.nmap", "네이버맵": "com.nhn.android.nmap",
    "카카오맵": "net.daum.android.map", "카카오내비": "com.locnall.KimGiSa",
    "티맵": "com.skt.tmap.ku", "tmap": "com.skt.tmap.ku",
    "구글지도": "com.google.android.apps.maps", "구글맵": "com.google.android.apps.maps",
    "스포티파이": "com.spotify.music", "spotify": "com.spotify.music",
    "멜론": "com.iloen.melon",
    "전화": "com.android.dialer", "메시지": "com.samsung.android.messaging",
    "인스타그램": "com.instagram.android", "인스타": "com.instagram.android",
    "크롬": "com.android.chrome", "chrome": "com.android.chrome",
}

# 미디어 키. 폰에서 뭐가 재생 중이든 시스템이 알아서 그 앱으로 보낸다.
_MEDIA_KEYS = {
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "stop": "KEYCODE_MEDIA_STOP",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
}


# --- ADB 실행 ---


def _exe() -> str | None:
    if settings.android_adb_path:
        return settings.android_adb_path
    return shutil.which("adb")


def _command(exe: str, args: list[str]) -> list[str]:
    """기기가 여럿 붙어 있을 수 있으니 시리얼이 지정돼 있으면 항상 고정한다."""
    base = [exe]
    if settings.android_serial:
        base += ["-s", settings.android_serial]
    return base + args


async def _adb(args: list[str], *, timeout: float = _TIMEOUT) -> tuple[bytes, str | None]:
    """(stdout, 오류메시지). adb 는 기기 문제도 종료코드 1 로 알려준다."""
    exe = _exe()
    if exe is None:
        return b"", SETUP_HINT

    try:
        proc = await asyncio.create_subprocess_exec(
            *_command(exe, args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        log.exception("adb 실행 실패")
        return b"", f"adb 를 실행하지 못했습니다: {exc}"

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return b"", "폰이 제때 응답하지 않았습니다."

    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip() or f"종료 코드 {proc.returncode}"
        lowered = detail.lower()
        if "no devices" in lowered or "device not found" in lowered or "offline" in lowered:
            return b"", SETUP_HINT
        if "unauthorized" in lowered:
            return b"", "폰 화면에 뜬 'USB 디버깅 허용' 을 눌러 주세요."
        if "more than one" in lowered:
            return b"", "기기가 여러 대 붙어 있습니다. .env 의 ANDROID_SERIAL 로 하나를 지정해 주세요."
        return b"", f"폰 명령이 실패했습니다: {detail[:300]}"

    return out, None


async def _shell(cmd: str, *, timeout: float = _TIMEOUT) -> tuple[str, str | None]:
    out, err = await _adb(["shell", cmd], timeout=timeout)
    return out.decode("utf-8", "replace"), err


# --- 앱 찾기 ---


def _norm(text: str) -> str:
    return text.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


async def _resolve_package(name: str) -> tuple[str | None, str | None]:
    key = _norm(name)
    if not key:
        return None, "어떤 앱인지 알려주세요."
    if key in _APP_ALIASES:
        return _APP_ALIASES[key], None
    if "." in name and " " not in name.strip():
        return name.strip(), None  # 이미 패키지명으로 보인다

    out, err = await _shell("pm list packages")
    if err:
        return None, err
    packages = [line.partition(":")[2].strip() for line in out.splitlines() if line.startswith("package:")]
    hits = [p for p in packages if key in _norm(p)]
    if not hits:
        return None, f"'{name}' 앱을 폰에서 찾지 못했습니다."
    # 이름이 짧을수록 본체일 가능성이 높다 (부가 패키지는 접미사가 붙는다).
    return min(hits, key=len), None


# --- 도구 ---


_NOTIF_FIELD = re.compile(r"android\.(title|text)=\w*(?:String|CharSequence)?\s*\((.*)\)\s*$")
_NOTIF_PKG = re.compile(r"\bpkg=([\w.]+)")


@tool
async def list_android_notifications(limit: int = 15) -> list[dict] | str:
    """폰에 떠 있는 알림을 읽는다. 카톡·문자·앱 알림을 확인할 때 쓴다.

    Args:
        limit: 최대 개수 (기본 15).
    """
    out, err = await _shell("dumpsys notification --noredact")
    if err:
        return err

    items: list[dict] = []
    pkg = ""
    current: dict[str, str] = {}

    for line in out.splitlines():
        if m := _NOTIF_PKG.search(line):
            # 새 알림 레코드가 시작됐다 — 앞서 모으던 것을 확정한다.
            if current.get("title") or current.get("text"):
                items.append({"app": pkg, **current})
            current = {}
            pkg = m.group(1)
            continue
        if m := _NOTIF_FIELD.search(line.strip()):
            field, value = m.group(1), m.group(2).strip()
            if value and value != "null":
                current[field] = value

    if current.get("title") or current.get("text"):
        items.append({"app": pkg, **current})

    if not items:
        return "폰에 떠 있는 알림이 없습니다."
    return items[: max(1, min(limit, 50))]


@tool
async def open_android_app(target: str) -> str:
    """폰에서 앱을 띄우거나 주소를 연다. 앱 이름은 한국어로 말해도 된다.

    Args:
        target: 앱 이름(예: 카카오톡, 유튜브, 티맵) 또는 http 로 시작하는 주소.
    """
    value = target.strip()
    if value.startswith(("http://", "https://")):
        cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(value)}"
        _, err = await _shell(cmd)
        return err or f"폰에서 주소를 열었습니다: {value}"

    package, err = await _resolve_package(value)
    if err:
        return err

    cmd = f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1"
    out, err = await _shell(cmd)
    if err:
        return err
    if "No activities found" in out or "Error" in out:
        return f"'{value}' 을(를) 띄우지 못했습니다. 폰에 설치돼 있는지 확인해 주세요."
    return f"폰에서 {value} 을(를) 열었습니다."


@tool
async def android_media(action: str) -> str:
    """폰의 음악·영상 재생을 조작한다. 지금 재생 중인 앱이 무엇이든 통한다.

    Args:
        action: play_pause / next / previous / stop / volume_up / volume_down 중 하나.
    """
    key = _MEDIA_KEYS.get(_norm(action))
    if key is None:
        return f"할 수 있는 동작: {', '.join(_MEDIA_KEYS)}"
    _, err = await _shell(f"input keyevent {key}")
    return err or f"폰: {action}"


@tool
async def android_screenshot() -> str:
    """폰 화면을 캡처해 파일로 저장한다. 화면에 뭐가 떠 있는지 확인해야 할 때 쓴다."""
    out, err = await _adb(["exec-out", "screencap", "-p"], timeout=30.0)
    if err:
        return err
    if not out.startswith(b"\x89PNG"):
        return "화면을 캡처하지 못했습니다."

    directory = Path(settings.captures_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"phone-{datetime.now():%Y%m%d-%H%M%S}.png"
    await asyncio.to_thread(path.write_bytes, out)
    return f"폰 화면을 저장했습니다: {path}"


@tool
async def send_sms(to: str, body: str) -> str:
    """폰의 메시지 앱에 문자를 채워 넣는다. 실행 전 사용자 확인을 거친다.

    기본은 '작성까지만' 이라 마지막 전송 버튼은 사람이 누른다. 자동 전송을 켜 두면
    (ANDROID_SMS_AUTOSEND) 보내기까지 한다.

    Args:
        to: 받는 사람 전화번호.
        body: 보낼 내용.
    """
    number = re.sub(r"[^\d+]", "", to)
    if not number:
        return "전화번호를 알아보지 못했습니다."
    if not body.strip():
        return "보낼 내용이 비어 있습니다."

    cmd = (
        "am start -a android.intent.action.SENDTO "
        f"-d {shlex.quote('smsto:' + number)} "
        f"--es sms_body {shlex.quote(body)} --ez exit_on_sent true"
    )
    _, err = await _shell(cmd)
    if err:
        return err

    if not settings.android_sms_autosend:
        return f"{number} 앞으로 문자를 띄워 뒀습니다. 폰에서 전송 버튼만 눌러 주세요."

    # 메시지 앱이 뜨고 입력칸에 내용이 채워질 때까지 잠깐 기다린다.
    await asyncio.sleep(1.5)
    # 전송 버튼으로 포커스를 옮긴 뒤 누른다. 앱 화면 구성에 기대는 방식이라
    # 기기에 따라 안 먹을 수 있고, 그때는 화면에 문자가 그대로 남는다.
    for key in ("KEYCODE_DPAD_RIGHT", "KEYCODE_ENTER"):
        _, err = await _shell(f"input keyevent {key}")
        if err:
            return f"문자는 작성됐지만 전송은 실패했습니다({err}). 폰에서 직접 눌러 주세요."
        await asyncio.sleep(0.4)
    return f"{number} 앞으로 문자를 보냈습니다. (폰 화면에서 전송됐는지 한 번 확인해 주세요.)"


@tool
async def place_call(to: str) -> str:
    """폰으로 전화를 건다. 실행 전 사용자 확인을 거친다.

    기본은 다이얼러에 번호만 채우는 것까지라, 마지막 통화 버튼은 사람이 누른다.
    운전 중처럼 화면을 못 볼 때 바로 걸리게 하려면 ANDROID_CALL_AUTODIAL 을 켠다.

    Args:
        to: 전화번호. 하이픈이 섞여 있어도 된다.
    """
    number = re.sub(r"[^\d+]", "", to)
    if not number:
        return "전화번호를 알아보지 못했습니다."

    # DIAL 은 번호만 채우고 멈춘다. CALL 은 바로 건다 — 잘못 걸면 되돌릴 수 없어서
    # 문자와 같은 이유로 기본값을 안전한 쪽에 둔다.
    action = "CALL" if settings.android_call_autodial else "DIAL"
    cmd = (
        f"am start -a android.intent.action.{action} "
        f"-d {shlex.quote('tel:' + number)}"
    )
    _, err = await _shell(cmd)
    if err:
        return err

    if not settings.android_call_autodial:
        return f"{number} 을(를) 다이얼러에 띄워 뒀습니다. 폰에서 통화 버튼만 눌러 주세요."
    return f"{number} 으로 전화를 걸었습니다."
