import asyncio
import base64
from email.mime.text import MIMEText

from langchain_core.tools import tool

from app.tools._google import SETUP_HINT, build_service


def _fetch_recent(max_results: int, query: str) -> list[dict] | str:
    service = build_service("gmail", "v1")
    if service is None:
        return SETUP_HINT

    listing = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results, q=query or None)
        .execute()
    )
    refs = listing.get("messages", [])
    if not refs:
        return []

    # 메시지별 메타데이터를 메일 수만큼 순차 get 하면 N+1 왕복이라 느리다. 한 번의 batch
    # HTTP 로 묶어 왕복을 1회로 줄인다. 응답은 콜백으로 비순차 도착하니 id 로 모았다가
    # 목록 순서대로 재조립한다(Gmail batch 상한 100, max_results 는 25라 한 배치에 들어간다).
    by_id: dict[str, dict] = {}

    def _collect(req_id: str, msg, exc):
        if exc is not None or msg is None:
            return
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        by_id[req_id] = {
            "id": req_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", "(제목 없음)"),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
        }

    batch = service.new_batch_http_request(callback=_collect)
    for ref in refs:
        batch.add(
            service.users().messages().get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ),
            request_id=ref["id"],
        )
    batch.execute()

    return [by_id[ref["id"]] for ref in refs if ref["id"] in by_id]


@tool
async def list_recent_emails(max_results: int = 10, query: str = "") -> list[dict] | str:
    """받은 편지함 메일을 조회한다.

    Args:
        max_results: 가져올 개수 (기본 10, 최대 25).
        query: Gmail 검색 문법 (예: 'is:unread', 'from:someone@x.com', 'newer_than:2d').

    Returns:
        [{id, from, subject, date, snippet}, ...]
    """
    return await asyncio.to_thread(_fetch_recent, max(1, min(max_results, 25)), query)


def _send(to: str, subject: str, body: str) -> dict | str:
    service = build_service("gmail", "v1")
    if service is None:
        return SETUP_HINT

    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"id": sent.get("id"), "to": to, "subject": subject}


@tool
async def send_email(to: str, subject: str, body: str) -> dict | str:
    """메일을 보낸다. 외부에 영향을 주므로 실행 전 사용자 확인을 거친다.

    Args:
        to: 받는 사람 주소.
        subject: 제목.
        body: 본문 (평문).
    """
    # googleapiclient 동기 호출을 워커 스레드로 빼 이벤트 루프를 막지 않는다.
    return await asyncio.to_thread(_send, to, subject, body)
