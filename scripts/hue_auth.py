"""필립스 휴 앱키 1회 발급.

브리지 가운데 링크 버튼을 누른 뒤 30초 안에 이 스크립트를 실행하면 앱키가 나온다.
나온 값을 .env 의 HUE_APP_KEY 에 넣으면 된다.

    python scripts/hue_auth.py

브리지를 자동으로 못 찾으면 주소를 인자로 준다.

    python scripts/hue_auth.py 192.168.0.12
"""

import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402

DISCOVERY_URL = "https://discovery.meethue.com"
DEVICE_TYPE = "javis#server"


def discover() -> str | None:
    try:
        res = httpx.get(DISCOVERY_URL, timeout=8.0)
        res.raise_for_status()
        for entry in res.json():
            if ip := entry.get("internalipaddress"):
                return str(ip)
    except Exception as exc:
        print(f"자동 탐색 실패: {exc}")
    return None


def main() -> None:
    ip = sys.argv[1] if len(sys.argv) > 1 else (settings.hue_bridge_ip or discover())
    if not ip:
        raise SystemExit(
            "브리지를 찾지 못했습니다. 공유기 관리 페이지에서 브리지 IP 를 확인해 "
            "인자로 넘겨 주세요:  python scripts/hue_auth.py 192.168.0.12"
        )

    print(f"브리지: {ip}")
    print("지금 브리지 가운데의 둥근 링크 버튼을 누르고 Enter 를 치세요.")
    input()

    # 브리지 인증서는 자체 서명이라 검증을 끈다. 같은 랜 안의 고정 장비다.
    res = httpx.post(
        f"https://{ip}/api",
        json={"devicetype": DEVICE_TYPE, "generateclientkey": True},
        verify=False,
        timeout=10.0,
    )
    body = res.json()
    entry = body[0] if isinstance(body, list) and body else {}

    if error := entry.get("error"):
        if error.get("type") == 101:
            raise SystemExit("링크 버튼을 먼저 누른 뒤 30초 안에 다시 실행해 주세요.")
        raise SystemExit(f"발급 실패: {error.get('description', error)}")

    key = (entry.get("success") or {}).get("username")
    if not key:
        raise SystemExit(f"응답을 이해하지 못했습니다: {body}")

    print("\n완료. .env 에 아래 두 줄을 넣으세요.\n")
    print(f"HUE_BRIDGE_IP={ip}")
    print(f"HUE_APP_KEY={key}")


if __name__ == "__main__":
    main()
