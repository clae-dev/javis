"""HUD 상태별 기본 에너지.

입자는 두 갈래로 움직인다 — 실제 소리(마이크 세기·목소리 포락선)가 있으면 그걸
따르고, 없으면 상태별 기본값으로 돌아간다. 그런데 기본값이 높으면 소리가 끊긴
순간에 오히려 크게 부풀어 오른다. 듣고 있는데 아무 말 안 할 때, 문장과 문장
사이가 그렇다 — 조용해질수록 화면이 흥분하는 꼴이라 실제로 그렇게 돌았다.

CSS·JS 는 브라우저에서만 도니 값만 뽑아 확인한다.
"""

import re
from pathlib import Path

import pytest

HUD_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "hud.js"

# 실제 소리가 실려 오는 상태. 여기는 기본값이 낮아야 한다.
SOUND_DRIVEN = ("listening", "speaking")


@pytest.fixture(scope="module")
def energy() -> dict[str, float]:
    text = HUD_JS.read_text(encoding="utf-8")
    m = re.search(r"const ENERGY = \{([^}]*)\}", text)
    assert m, "hud.js 에서 ENERGY 를 찾지 못했다"
    return {
        k: float(v)
        for k, v in re.findall(r"(\w+)\s*:\s*([\d.]+)", m.group(1))
    }


def test_every_state_has_a_baseline(energy):
    for state in ("idle", "listening", "thinking", "speaking", "error"):
        assert state in energy, state


@pytest.mark.parametrize("state", SOUND_DRIVEN)
def test_sound_driven_states_stay_calm_when_quiet(energy, state):
    """소리가 끊겼을 때 idle 보다 크게 부풀면 안 된다."""
    assert energy[state] <= 0.35, (
        f"{state} 기본값이 {energy[state]} 다. 소리가 끊기면 이 값으로 돌아가므로, "
        "높게 두면 조용할 때 오히려 입자가 커진다."
    )


def test_thinking_can_stay_lively(energy):
    """생각 중에는 실을 소리가 없다. 여기까지 낮추면 화면이 죽어 보인다."""
    assert energy["thinking"] > energy["idle"]


def test_mic_and_envelope_take_priority_over_the_baseline():
    """draw() 가 기본값보다 실제 소리를 먼저 보는지."""
    text = HUD_JS.read_text(encoding="utf-8")
    body = text[text.index("function draw()") :]
    envelope_at = body.index("readEnvelope()")
    mic_at = body.index("readMic()")
    fallback_at = body.index("targetEnergy - energy")
    assert envelope_at < fallback_at and mic_at < fallback_at
