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


def test_ws_open_when_token_disabled(open_client):
    """인증이 꺼져 있으면 첫 프레임을 인증으로 소비하지 않는다."""
    with open_client.websocket_connect("/ws/hud") as ws:
        ws.send_text("ping")
