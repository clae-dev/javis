"""Claude Code 브리지.

`claude -p` 를 서브프로세스로 돌려 등록된 프로젝트의 코드를 읽거나 고친다. 프로젝트마다
대화 세션(session_id)을 보관해, 다음 요청이 앞선 맥락 위에서 이어진다. 운전 중처럼
화면을 못 볼 때 말로 코딩하려고 만든 통로다.

임의 경로가 열리지 않도록 CODE_PROJECTS 에 등록한 프로젝트만 허용하고, 수정 작업에도
Bash 는 주지 않는다 — 음성 한 마디로 임의 명령이 돌면 안 된다.
"""

import asyncio
import json
import logging
import shutil
from pathlib import Path

from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger("javis.code")

SETUP_HINT = (
    "Claude Code 연동이 설정되지 않았습니다. claude CLI 를 설치하고 "
    ".env 의 CODE_PROJECTS 에 프로젝트를 등록해 주세요."
)

# 조회용. 파일을 건드릴 수단을 아예 주지 않는다.
_READ_TOOLS = "Read,Grep,Glob"
# 수정용. Bash 는 일부러 뺐다(빌드·git 은 사람이 직접).
_EDIT_TOOLS = "Read,Grep,Glob,Edit,Write"

# 결과는 음성으로 읽힌다. 길게 늘어놓으면 듣는 사람이 놓친다.
_ASK_STYLE = "한국어로 답해라. 핵심만 다섯 문장 이내로, 코드 블록은 꼭 필요할 때만 쓴다."
_EDIT_STYLE = "한국어로 답해라. 작업이 끝나면 무엇을 왜 바꿨는지 세 문장 이내로 요약해라."

_TIMEOUT = 180.0

_projects_cache: dict[str, Path] | None = None
# 같은 프로젝트에 두 요청이 겹치면 세션이 꼬인다. 프로젝트별로만 직렬화한다.
_locks: dict[str, asyncio.Lock] = {}


def _projects() -> dict[str, Path]:
    """CODE_PROJECTS 를 {이름: 경로} 로 판다.

    `javis=C:\\workspace\\Javis,pet=C:\\...` 형식. 한 프로젝트에 이름을 여러 개 달려면
    `|` 로 묶는다 — `javis|자비스=C:\\workspace\\Javis`. 말로 부르는 이름(한글)과
    폴더 이름(영문)이 다를 때 쓴다.
    """
    global _projects_cache
    if _projects_cache is None:
        parsed: dict[str, Path] = {}
        for entry in settings.code_projects.split(","):
            names, sep, path = entry.partition("=")
            path = path.strip()
            if not sep or not path:
                continue
            for name in names.split("|"):
                if name := name.strip():
                    parsed[name] = Path(path)
        _projects_cache = parsed
    return _projects_cache


def project_names() -> list[str]:
    """프로젝트별 대표 이름. 시스템 프롬프트에 실어 에이전트가 맞는 이름을 고르게 한다.

    별칭은 첫 번째 것만 보여준다 — 같은 프로젝트가 두 번 나열되면 헷갈리기만 한다.
    """
    seen: dict[str, str] = {}
    for name, path in _projects().items():
        seen.setdefault(str(path), name)
    return list(seen.values())


def _norm(text: str) -> str:
    return text.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _resolve(name: str) -> tuple[str, Path] | None:
    """프로젝트 이름을 찾아 (이름, 경로) 를 준다.

    음성 인식은 이름을 정확히 못 받아적으므로 부분 일치까지 본다.
    """
    key = _norm(name)
    if not key:
        return None
    projects = _projects()
    for project, path in projects.items():
        if _norm(project) == key:
            return project, path
    for project, path in projects.items():
        norm = _norm(project)
        if key in norm or norm in key:
            return project, path
    return None


# --- 세션 저장 ---


def _sessions_path() -> Path:
    return Path(settings.code_sessions_path)


def _load_sessions() -> dict[str, str]:
    path = _sessions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("코드 세션 파일을 읽지 못했습니다(무시): %s", exc)
        return {}


def _save_sessions(sessions: dict[str, str]) -> None:
    path = _sessions_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("코드 세션 저장 실패(무시): %s", exc)


# --- CLI 호출 ---


def _command(exe: str, args: list[str]) -> list[str]:
    """실행할 명령을 만든다.

    npm 설치본은 `claude.cmd` 라 CreateProcess 로 직접 못 띄운다. 그때만 cmd.exe 를 거친다.
    네이티브 설치본(claude.exe)은 그대로 실행한다.
    """
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *args]
    return [exe, *args]


async def _invoke(
    exe: str,
    cwd: Path,
    prompt: str,
    tools: str,
    style: str,
    permission_mode: str | None,
    session_id: str | None,
    *,
    retry: bool = True,
) -> tuple[str, str | None]:
    """claude 를 한 번 돌리고 (결과 텍스트, 새 session_id) 를 돌려준다."""
    args = [
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        settings.code_model,
        "--tools",
        tools,
        "--append-system-prompt",
        style,
    ]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if session_id:
        args += ["--resume", session_id]

    try:
        proc = await asyncio.create_subprocess_exec(
            *_command(exe, args),
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        log.exception("claude 실행 실패")
        return f"코드 작업을 실행하지 못했습니다: {exc}", session_id

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "작업이 3분 안에 끝나지 않았어요. 조금 뒤에 다시 물어봐 주세요.", session_id

    stdout = out.decode("utf-8", "replace").strip()
    stderr = err.decode("utf-8", "replace").strip()

    if proc.returncode != 0:
        # 저장해 둔 세션이 깨졌으면(파일 정리·버전 변경 등) 새 세션으로 한 번만 다시 해 본다.
        lowered = stderr.lower()
        if retry and session_id and ("session" in lowered or "resume" in lowered):
            log.warning("코드 세션 손상, 새 세션으로 재시도: %s", stderr[:200])
            return await _invoke(
                exe, cwd, prompt, tools, style, permission_mode, None, retry=False
            )
        detail = stderr or stdout or f"종료 코드 {proc.returncode}"
        return f"코드 작업이 실패했습니다: {detail[:500]}", session_id

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # 포맷이 바뀌어 JSON 이 아니어도 내용은 살린다.
        return stdout or "(응답 없음)", session_id

    result = str(data.get("result") or "").strip() or "(응답 없음)"
    if data.get("is_error"):
        result = f"작업 중 문제가 있었습니다: {result}"
    if (cost := data.get("total_cost_usd")) is not None:
        log.info("claude 호출 비용 $%.4f (%s)", cost, cwd.name)
    return result, data.get("session_id") or session_id


async def _run(project_hint: str, prompt: str, tools: str, style: str, permission_mode: str | None) -> str:
    exe = shutil.which("claude")
    if exe is None or not settings.code_projects:
        return SETUP_HINT

    hit = _resolve(project_hint)
    if hit is None:
        names = ", ".join(_projects()) or "(등록된 프로젝트가 없습니다)"
        return f"'{project_hint}' 프로젝트를 찾을 수 없습니다. 등록된 프로젝트: {names}"

    project, cwd = hit
    if not cwd.is_dir():
        return f"'{project}' 의 경로를 찾을 수 없습니다: {cwd}"

    # 별칭이 여럿이어도(javis / 자비스) 같은 프로젝트면 한 세션·한 자물쇠를 쓰도록 경로로 묶는다.
    key = str(cwd)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        sessions = await asyncio.to_thread(_load_sessions)
        result, session_id = await _invoke(
            exe, cwd, prompt, tools, style, permission_mode, sessions.get(key)
        )
        if session_id and sessions.get(key) != session_id:
            sessions[key] = session_id
            await asyncio.to_thread(_save_sessions, sessions)
    return result


# --- 도구 ---


@tool
async def ask_project(project: str, question: str) -> str:
    """등록된 코드 프로젝트를 읽고 질문에 답한다. 파일은 수정하지 않는다.

    "자비스 프로젝트에서 도구가 몇 개야?", "로그인 처리 어디서 해?" 처럼 코드베이스를
    직접 봐야 답할 수 있는 질문에 쓴다. 같은 프로젝트로 이어 물으면 앞선 맥락이 유지된다.

    Args:
        project: 프로젝트 이름 (설정에 등록된 것. 예: javis).
        question: 물어볼 내용. 한 문장으로 구체적으로.
    """
    return await _run(project, question, _READ_TOOLS, _ASK_STYLE, None)


@tool
async def edit_project(project: str, request: str) -> str:
    """등록된 코드 프로젝트의 파일을 실제로 수정한다. 실행 전 사용자 확인을 거친다.

    파일을 읽고 고치는 것만 한다. 빌드·테스트·git 같은 명령 실행은 하지 않으니,
    그런 요청이면 사용자에게 직접 하라고 안내해라.

    Args:
        project: 프로젝트 이름 (설정에 등록된 것. 예: javis).
        request: 무엇을 어떻게 바꿀지. 파일이나 위치를 알면 같이 적는다.
    """
    return await _run(project, request, _EDIT_TOOLS, _EDIT_STYLE, "acceptEdits")
