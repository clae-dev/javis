"""Google API 서비스 빌더 (캘린더 / Gmail 공용).

credentials/google.json (OAuth 클라이언트) 과 credentials/token.json
(scripts/google_auth.py 로 1회 발급) 을 쓴다. 둘 중 하나라도 없으면 None 을
돌려주고, 도구는 SETUP_HINT 로 안내한다.
"""

import os
import threading

from app.config import GOOGLE_SCOPES, settings

SETUP_HINT = (
    "Google 연결이 안 되어 있습니다. README의 'Google 연결'을 따라 "
    "credentials/google.json 을 두고 `python scripts/google_auth.py` 를 한 번 실행해 주세요."
)

# 서비스(httplib2 전송)는 스레드 비안전이라 공유하면 안 된다. 도구는 to_thread 워커에서
# 호출되고 그 풀은 스레드를 재사용하므로, 스레드별로 캐시하면 재사용은 챙기되 동시 호출 간
# 객체 공유는 피한다. 자격증명은 만료 시 요청 단에서 자동 refresh 되어 재사용해도 안전하다.
_local = threading.local()


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not os.path.exists(settings.google_token_path):
        return None

    creds = Credentials.from_authorized_user_file(settings.google_token_path, GOOGLE_SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(settings.google_token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    if not creds.valid:
        return None
    return creds


def build_service(api: str, version: str):
    """(api, version)별 서비스 객체를 스레드 로컬로 캐시한다.

    build() 는 디스커버리 객체를 구성하느라 무거운데, 기존엔 도구 호출마다 토큰을 읽고
    이걸 새로 만들었다. 캐시된 서비스는 자격증명을 자동 refresh 하므로 재사용해도 된다.
    """
    from googleapiclient.discovery import build

    cache = getattr(_local, "services", None)
    if cache is None:
        cache = _local.services = {}

    key = (api, version)
    service = cache.get(key)
    if service is not None:
        return service

    creds = _load_credentials()
    if creds is None:
        return None
    service = build(api, version, credentials=creds, cache_discovery=False)
    cache[key] = service
    return service
