"""Google API 서비스 빌더 (캘린더 / Gmail 공용).

credentials/google.json (OAuth 클라이언트) 과 credentials/token.json
(scripts/google_auth.py 로 1회 발급) 을 쓴다. 둘 중 하나라도 없으면 None 을
돌려주고, 도구는 SETUP_HINT 로 안내한다.
"""

import os

from app.config import GOOGLE_SCOPES, settings

SETUP_HINT = (
    "Google 연결이 안 되어 있습니다. README의 'Google 연결'을 따라 "
    "credentials/google.json 을 두고 `python scripts/google_auth.py` 를 한 번 실행해 주세요."
)


def build_service(api: str, version: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not os.path.exists(settings.google_token_path):
        return None

    creds = Credentials.from_authorized_user_file(settings.google_token_path, GOOGLE_SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(settings.google_token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    if not creds.valid:
        return None
    return build(api, version, credentials=creds, cache_discovery=False)
