"""Google OAuth 1회 인증 (캘린더 + Gmail).

credentials/google.json (OAuth 데스크톱 클라이언트) 을 두고 이 스크립트를 실행하면
브라우저가 열리고, 동의 후 credentials/token.json 이 생성된다.

    python scripts/google_auth.py
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import GOOGLE_SCOPES, settings  # noqa: E402

SCOPES = GOOGLE_SCOPES


def main() -> None:
    if not os.path.exists(settings.google_credentials_path):
        raise SystemExit(
            f"{settings.google_credentials_path} 가 없습니다. "
            "Google Cloud Console에서 OAuth 데스크톱 클라이언트를 만들어 이 경로에 두세요."
        )

    flow = InstalledAppFlow.from_client_secrets_file(settings.google_credentials_path, SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(settings.google_token_path), exist_ok=True)
    with open(settings.google_token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"완료. 토큰 저장: {settings.google_token_path}")


if __name__ == "__main__":
    main()
