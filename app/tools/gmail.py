import base64
from email.mime.text import MIMEText

from langchain_core.tools import tool

from app.tools._google import SETUP_HINT, build_service


@tool
async def list_recent_emails(max_results: int = 10, query: str = "") -> list[dict] | str:
    """받은 편지함 메일을 조회한다.

    Args:
        max_results: 가져올 개수 (기본 10, 최대 25).
        query: Gmail 검색 문법 (예: 'is:unread', 'from:someone@x.com', 'newer_than:2d').

    Returns:
        [{id, from, subject, date, snippet}, ...]
    """
    service = build_service("gmail", "v1")
    if service is None:
        return SETUP_HINT

    max_results = max(1, min(max_results, 25))
    listing = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results, q=query or None)
        .execute()
    )

    out: list[dict] = []
    for ref in listing.get("messages", []):
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append(
            {
                "id": ref["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", "(제목 없음)"),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            }
        )
    return out


@tool
async def send_email(to: str, subject: str, body: str) -> dict | str:
    """메일을 보낸다. 외부에 영향을 주므로 실행 전 사용자 확인을 거친다.

    Args:
        to: 받는 사람 주소.
        subject: 제목.
        body: 본문 (평문).
    """
    service = build_service("gmail", "v1")
    if service is None:
        return SETUP_HINT

    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"id": sent.get("id"), "to": to, "subject": subject}
