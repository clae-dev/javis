"""오래 걸리는 일을 맡겨 두는 도구."""

from langchain_core.tools import tool

from app import background


@tool
async def start_background_task(request: str) -> str:
    """오래 걸리는 일을 맡겨 두고 즉시 돌아온다. 다 되면 알림으로 결과를 알려준다.

    "~하는 동안 …해 둬", "시간 걸려도 되니까 …" 처럼 지금 당장 답을 기다리지 않아도 되는
    일에 쓴다. 여러 페이지를 뒤지거나 검색을 반복해야 하는 조사, 자료 수집 같은 것.
    반대로 몇 초면 끝나는 일은 그냥 직접 하면 된다 — 여기 맡기면 오히려 느려진다.

    맡긴 일은 승인해 줄 사람이 없는 상태로 도니까, 메일 전송처럼 확인이 필요한 작업은
    실행되지 않고 무엇이 막혔는지만 결과에 담겨 온다.

    Args:
        request: 시킬 일을 한 문장으로. 나중에 혼자 읽어도 알 수 있게 구체적으로 쓴다.
    """
    request = request.strip()
    if not request:
        return "무엇을 할지 알려 주세요."

    task_id = await background.start(request)
    return (
        f"맡았습니다(#{task_id}). 다 되면 알려 드릴게요. "
        "중간에 궁금하면 진행 상황을 물어보셔도 됩니다."
    )


@tool
async def list_background_tasks(limit: int = 10) -> list[dict] | str:
    """맡겨 둔 일들이 어떻게 되고 있는지 본다.

    status 는 running(도는 중) / done(끝남) / failed(실패) / interrupted(서버 재시작으로 중단).

    Args:
        limit: 최근 몇 건까지 볼지.
    """
    rows = await background.listing(max(1, min(int(limit), 30)))
    return rows or "맡겨 둔 일이 없습니다."
