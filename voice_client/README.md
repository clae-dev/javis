# 자비스 음성 데몬

"자비스"라고 부르면 깨어나 명령을 듣고, 처리한 뒤 목소리로 답하는 PC 상주 프로그램.
브라우저가 아니라 OS 마이크를 직접 쓰므로 보안 컨텍스트(HTTPS) 제약이 없다.

깨우는 말 감지는 **로컬(Vosk)** 에서만 일어난다 — 깨우기 전에는 음성을 밖으로 보내지 않는다.

## 설치

백엔드가 먼저 떠 있어야 한다:

```bash
docker compose up -d
```

음성 데몬 의존성 설치 (PC 파이썬):

```bash
py -m pip install -r voice_client/requirements.txt
```

## 실행

```bash
py voice_client/jarvis_voice.py
```

- 최초 실행 시 한국어 음성 모델(~50MB)을 자동 다운로드한다.
- `대기 중…` 이 뜨면 **"자비스"** 라고 불러본다. → 띵 소리 후 명령을 듣는다.
- 메일 전송·일정 생성처럼 확인이 필요한 작업은 음성으로 "네/아니오"를 묻는다.
- 종료: `Ctrl+C`

## 설정 (환경변수, 선택)

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `JAVIS_BACKEND` | 백엔드 주소 | `http://localhost:8000` |
| `JAVIS_WAKE` | 깨우는 말(쉼표 구분) | `자비스,자비,차비스,장비스,jarvis` |

깨우기가 잘 안 되거나 오탐이 잦으면 `JAVIS_WAKE` 를 조절한다.

## 부팅 시 자동 실행 (선택)

`Win+R` → `shell:startup` 으로 시작프로그램 폴더를 열고, 아래 내용의 `jarvis.bat` 을 넣는다:

```bat
@echo off
cd /d C:\workspace\Javis
py voice_client\jarvis_voice.py
```

## 한계 / 메모

- 반응: 깨우기(로컬, 즉시) → STT·LLM·TTS(클라우드)라 한 번 답하는 데 보통 몇 초.
- 작은 모델이라 깨우는 말에 가끔 오탐이 있다. 정확도가 더 필요하면 Porcupine 으로 교체 가능.
- 자기 TTS 소리를 마이크가 되먹지 않도록, 말한 직후 입력 버퍼를 비운다.
