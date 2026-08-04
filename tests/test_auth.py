"""접속 인증.

토큰을 켠 상태에서 REST 헤더와 WebSocket 첫 프레임이 각각 막히는지 본다.
DB 가 필요한 경로는 건드리지 않는다 — 인증은 그 앞에서 끝나야 정상이다.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import hud, rest
from app.config import settings

TOKEN = "test-token-abc"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "javis_token", TOKEN)
    app = FastAPI()
    app.include_router(rest.router)
    app.include_router(hud.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def open_client(monkeypatch):
    """토큰을 비운 상태 — 로컬 단독 실행."""
    monkeypatch.setattr(settings, "javis_token", "")
    app = FastAPI()
    app.include_router(rest.router)
    app.include_router(hud.router)
    with TestClient(app) as c:
        yield c


def test_health_is_open(client):
    assert client.get("/health").status_code == 200


def test_rest_rejects_missing_token(client):
    assert client.get("/memories").status_code == 401


def test_rest_rejects_wrong_token(client):
    res = client.get("/memories", headers={"X-Javis-Token": "nope"})
    assert res.status_code == 401


def test_rest_accepts_header_token(client):
    res = client.post("/hud/event", json={"state": "idle"}, headers={"X-Javis-Token": TOKEN})
    assert res.status_code == 200


def test_rest_accepts_bearer_token(client):
    res = client.post("/hud/event", json={"state": "idle"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 200


def test_ws_accepts_token_in_first_frame(client):
    with client.websocket_connect("/ws/hud") as ws:
        ws.send_text('{"token": "%s"}' % TOKEN)
        ws.send_text("ping")  # 통과했으면 서버가 조용히 삼킨다


def test_ws_rejects_wrong_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/hud") as ws:
            ws.send_text('{"token": "nope"}')
            ws.receive_text()
    assert exc.value.code == 1008


def test_podcast_requires_token(client):
    assert client.get("/podcast/x.mp3").status_code == 401


def test_podcast_serves_a_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "podcasts_path", str(tmp_path))
    (tmp_path / "회고.mp3").write_bytes(b"ID3audio")

    res = client.get("/podcast/회고.mp3", headers={"X-Javis-Token": TOKEN})
    assert res.status_code == 200
    assert res.content == b"ID3audio"


@pytest.mark.parametrize("name", ["../../.env", "..%2F.env", "sub/x.mp3", "노트.md", "없음.mp3"])
def test_podcast_refuses_anything_but_its_own_mp3(client, tmp_path, monkeypatch, name):
    """파일명은 URL 로 들어온다. 디렉터리를 빠져나가거나 다른 파일을 집으면 안 된다."""
    monkeypatch.setattr(settings, "podcasts_path", str(tmp_path))
    (tmp_path / "노트.md").write_text("비밀", encoding="utf-8")
    (tmp_path.parent / ".env").write_text("OPENAI_API_KEY=leak", encoding="utf-8")

    res = client.get(f"/podcast/{name}", headers={"X-Javis-Token": TOKEN})
    assert res.status_code == 404
    assert b"leak" not in res.content


def test_ws_open_when_token_disabled(open_client):
    """인증이 꺼져 있으면 첫 프레임을 인증으로 소비하지 않는다."""
    with open_client.websocket_connect("/ws/hud") as ws:
        ws.send_text("ping")
