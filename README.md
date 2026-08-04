# Javis

개인용 AI 비서. 매일 쓰려고 만든 물건이라 화려한 기능보다 **안 끊기고, 빠르고, 믿을 수 있는 것**을 먼저 챙겼다.

텍스트·음성으로 대화하고, 캘린더·메일·웹 검색·리마인더 같은 도구를 직접 골라 쓰며, 대화에서 기억할 만한 것만 추려 장기 기억으로 쌓는다. 외부에 영향을 주는 작업(메일 전송·일정 생성)은 실행 전에 반드시 확인을 받는다.

## 할 수 있는 것

- **대화**: 스트리밍 응답. 토큰이 나오는 즉시 화면에 흐른다.
- **장기 기억**: 대화 끝에 기억할 사실만 추려 pgvector에 저장하고, 다음 대화에서 검색해 참고한다.
- **리마인더**: 등록/조회/완료. 마감 시각이 되면 스케줄러가 알림을 push 한다.
- **캘린더**: 일정 조회·생성(생성은 확인 필요).
- **Gmail**: 메일 조회·전송(전송은 확인 필요).
- **웹 검색**: 최신 정보가 필요할 때. Tavily 키가 있으면 쓰고, 없으면 무료 폴백.
- **코드**: 등록한 프로젝트를 Claude Code로 읽고 고친다. 수정은 확인 필요.
- **조명**: 필립스 휴 조명·방을 켜고 끄고 밝기·색을 바꾼다(확인 필요).
- **폰**: 안드로이드폰의 알림을 읽고, 앱을 띄우고, 음악을 넘기고, 화면을 캡처하고, 문자를 쓴다(문자는 확인 필요).
- **브라우저**: 자바스크립트로 그려지는 페이지를 실제로 열어 읽고, 목록을 긁고, 필요하면 조작한다(조작은 확인 필요).
- **유튜브**: 영상 검색, 조회수·좋아요 같은 지표, 채널 최근 영상.
- **음성**: 마이크로 말하면 Whisper로 받아쓰고, 응답을 TTS로 읽어준다. 받아쓰기는 로컬로도 돌릴 수 있다.
- **능동 알림**: 마감된 리마인더, 아침 일정 브리핑을 먼저 알려준다.
- **정기 작업**: "매일 아침 새 매물 확인해줘" 처럼 걸어 두면, 그 시각에 실제로 알아보고 결과를 알려준다.
- **PWA**: 휴대폰/데스크톱에 설치 가능. 브라우저 알림 지원.

## 구성

```
브라우저 / 설치형 PWA
        │  WebSocket (텍스트 스트리밍, 능동 알림)  +  REST (음성 STT/TTS)
   FastAPI Gateway
        │
   LangGraph Agent ── PostgreSQL + pgvector (장기 기억 / 대화 체크포인트)
        │
   Tools: 시간, 웹검색, 기억, 리마인더, 정기작업, 캘린더, Gmail,
          코드(Claude Code CLI), 휴 조명, 안드로이드(ADB), 브라우저(Playwright), 유튜브
        ↑
   APScheduler (능동 알림 + 사용자가 걸어 둔 정기 작업)
```

흐름은 **prepare(프로필·기억 동시 조회) → 에이전트 → 도구 실행 → 반추(reflect)**. 도구가 필요하면 ReAct 루프를 돌고, 쓰기 작업은 LangGraph `interrupt`로 그래프 흐름 안에서 확인을 받는다. 확인은 웹 화면·음성("네/아니오") 양쪽에 다 연결돼 있다.

반추는 응답을 내보낸 뒤 백그라운드로 돈다. 사용자가 기억 저장을 기다릴 이유가 없어서다.

대화 체크포인트는 가능하면 Postgres에 영속화해 재시작 후에도 맥락과 보류 중인 확인이 살아남는다(실패 시 인메모리로 자동 폴백).

## 빠른 시작 (Docker)

```bash
cp .env.example .env          # OPENAI_API_KEY 채우기
docker compose up --build
```

브라우저에서 http://localhost:8000 접속. 바로 채팅·음성 테스트 가능하고, 우상단에서 PWA로 설치할 수 있다.

## 로컬 실행 (Docker 없이)

Postgres(pgvector 확장)와 Python 3.12가 필요하다.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r app/requirements.txt
# .env 의 DATABASE_URL 을 로컬 Postgres 로 맞춘 뒤
uvicorn app.main:app --reload
```

## 환경 변수

| 키 | 설명 | 기본값 |
|----|------|--------|
| `OPENAI_API_KEY` | OpenAI 키 (채팅·임베딩·음성) | (필수) |
| `LLM_MODEL` | 메인 응답 모델 | `gpt-4o` |
| `FAST_MODEL` | 의도 분류·반추용 경량 모델 | `gpt-4o-mini` |
| `EMBEDDING_MODEL` | 임베딩 모델 | `text-embedding-3-small` |
| `STT_MODEL` / `TTS_MODEL` / `TTS_VOICE` | 음성 모델·목소리 | whisper-1 / gpt-4o-mini-tts / alloy |
| `STT_ENGINE` / `STT_LOCAL_MODEL` | `local` 이면 faster-whisper로 이 기계에서 받아쓴다 | `openai` / `small` |
| `TAVILY_API_KEY` | 웹 검색용(선택, 없으면 무료 폴백) | (빈값) |
| `JAVIS_TOKEN` | 접속 토큰. **비우면 인증 없이 열린다** | (빈값) |
| `CODE_PROJECTS` | 음성으로 다룰 코드 프로젝트 `이름=경로` 목록 | (빈값) |
| `CODE_MODEL` | 코드 작업에 쓸 Claude 모델 | `opus` |
| `HUE_BRIDGE_IP` / `HUE_APP_KEY` | 필립스 휴(선택). IP를 비우면 자동 탐색 | (빈값) |
| `ANDROID_ADB_PATH` / `ANDROID_SERIAL` | 안드로이드(선택). adb 경로·기기 지정 | (빈값) |
| `ANDROID_SMS_AUTOSEND` | 문자를 보내기까지 할지 | `false` |
| `BROWSER_USER_DATA_DIR` / `BROWSER_HEADLESS` | 브라우저 프로필 위치·헤드리스 여부 | `credentials/browser` / `true` |
| `YOUTUBE_API_KEY` | 유튜브 데이터 API(선택) | (빈값) |
| `DATABASE_URL` | Postgres 접속 문자열 | compose 기본값 |
| `TIMEZONE` | 스케줄러·시간 표시 기준 | `Asia/Seoul` |
| `USE_POSTGRES_CHECKPOINTER` | 대화 영속화 | `true` |
| `ENABLE_SCHEDULER` | 능동 알림 | `true` |
| `OWNER_NAME` / `ASSISTANT_NAME` | 사용자·비서 이름 | `창래` / `자비스` |

## 접속 토큰

`JAVIS_TOKEN` 을 채우면 REST는 `X-Javis-Token`(또는 `Authorization: Bearer`) 헤더를, WebSocket은 **첫 프레임**을 토큰으로 요구한다. 토큰을 쿼리파라미터로 받지 않는 건 URL이 접속 로그·프록시 기록에 그대로 남기 때문이다.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 값 하나 만들어 .env 에
```

- 비워 두면 인증 없이 열린다. **이 기계에서 혼자 쓸 때만** 그렇게 두고, 폰이나 다른 기기에서 붙을 거면 반드시 채운다.
- 웹 화면은 처음 붙을 때 토큰을 한 번 묻고 브라우저에 저장한다. 음성 데몬은 같은 `JAVIS_TOKEN` 환경변수를 읽는다.
- `GET /health` 만 열려 있다 — 살아있는지 확인하는 용도다.

## Google 연결 (캘린더 · Gmail)

1. Google Cloud Console에서 OAuth 클라이언트(데스크톱 앱)를 만들어 `credentials/google.json`으로 저장한다. Calendar API와 Gmail API를 활성화한다.
2. 최초 1회 인증(캘린더+Gmail 스코프 동시 동의):

   ```bash
   python scripts/google_auth.py
   ```

   브라우저가 열리고, 끝나면 `credentials/token.json`이 생긴다.
3. 이후 자비스가 일정·메일을 다룰 수 있다. 일정 생성과 메일 전송은 항상 확인을 거친다.

자격증명이 없으면 해당 도구만 "설정이 필요합니다"라고 답하고 나머지는 정상 동작한다.

## 코드 연결 (Claude Code)

말로 코드를 다루기 위한 통로. 자비스가 `claude -p` 를 대신 돌려 프로젝트를 읽거나 고친다.
운전·육아처럼 화면을 못 볼 때 쓰려고 만들었다.

1. [Claude Code](https://claude.com/claude-code)를 설치하고 로그인해 둔다 (`claude --version` 으로 확인).
2. `.env` 에 다룰 프로젝트를 등록한다. **여기 적은 것만 열린다.**

   ```bash
   CODE_PROJECTS=javis|자비스=C:\workspace\Javis,펫=C:\workspace\DomabaemPet
   ```

   말로 부르는 이름이 폴더명과 다르면 `|` 로 별칭을 여러 개 단다("자비스 프로젝트에서…").
3. **백엔드를 Docker 밖에서 띄운다.** 컨테이너 안에는 `claude` 도 프로젝트 경로도 없다:

   ```bash
   docker compose up -d postgres     # DB만 컨테이너로
   uvicorn app.main:app --reload     # 앱은 호스트에서
   ```

동작 방식과 한계:

- **조회**(`ask_project`)는 읽기 도구(Read·Grep·Glob)만 받는다. 파일을 건드릴 수 없다.
- **수정**(`edit_project`)은 실행 전 확인을 거치고, 파일 수정만 한다. **셸 명령은 주지 않는다** — 빌드·테스트·git 은 직접 해야 한다. 말 한 마디로 임의 명령이 도는 걸 막기 위해서다.
- 프로젝트별로 대화 세션이 `credentials/code_sessions.json` 에 남아, 이어 물으면 맥락이 유지된다.
- `claude` 가 없거나 `CODE_PROJECTS` 가 비면 해당 도구만 안내 문구를 돌려주고 나머지는 정상 동작한다.

## 집·폰 연결

### 필립스 휴

브리지의 로컬 API를 직접 부른다. 클라우드를 거치지 않아 인터넷이 끊겨도 집 안에서는 동작한다.

```bash
python scripts/hue_auth.py     # 브리지 가운데 버튼을 누르고 실행
```

나온 `HUE_BRIDGE_IP` / `HUE_APP_KEY` 를 `.env` 에 넣으면 끝이다. 조명 하나뿐 아니라 방(그룹)도 같은 이름으로 부를 수 있어서 "거실 꺼줘" 한 마디에 그 방 전체가 꺼진다.

### 안드로이드폰 (ADB)

폰의 개발자 옵션에서 USB 디버깅을 켜고 케이블로 연결하거나, 무선 디버깅을 켠 뒤 한 번만 붙여 둔다.

```bash
adb connect 192.168.0.20:5555
adb devices                    # 기기가 여럿이면 ANDROID_SERIAL 로 하나 고정
```

문자는 기본적으로 **작성까지만** 한다 — 메시지 앱에 내용을 채워 두고 전송 버튼은 사람이 누른다. `ANDROID_SMS_AUTOSEND=true` 로 켜면 보내기까지 하지만, 앱 화면 구성에 기대는 방식이라 기기에 따라 안 먹을 수 있다.

## 브라우저 자동화

검색 요약으로 부족하고 페이지 안을 직접 봐야 할 때, 또는 자바스크립트로 그려져서 그냥 받아오면 비어 있는 페이지에 쓴다.

```bash
pip install playwright
python -m playwright install chromium
```

- 백엔드가 브라우저를 띄워야 하므로 **Docker 밖(호스트)에서** 돌려야 한다. 코드 연결과 같은 조건이다.
- 로그인 세션이 `credentials/browser/` 에 남아 한 번 로그인해 두면 다음에도 유지된다. **이 폴더는 공유하면 안 된다** (`.gitignore` 에 들어 있다).
- 가져온 페이지 내용은 항상 `<외부자료>` 로 감싸서 모델에 넘긴다. 남이 쓴 글에 "이전 지시를 무시하고…" 같은 문장이 섞여 있어도 명령으로 읽히지 않게 하기 위해서다. 시스템 프롬프트에도 같은 규칙을 박아 뒀고, 실제로 페이지를 건드리는 `browser_act` 는 쓰기 도구라 매번 확인을 거친다.
- 결제·주문·송금은 도구에 아예 넣지 않았다.

## 정기 작업

리마인더가 *적어 둔 문장을 그 시각에 읽어 주는* 것이라면, 정기 작업은 *그 시각에 가서 직접 해 보는* 것이다.

> "매일 아침 8시에 관심 지역 새 매물 확인해서 알려줘"

- `scheduled_jobs` 테이블에 남고, 앱이 다시 떠도 복원된다.
- 실행마다 대화 맥락을 새로 시작한다. 앞선 실행에 확인 대기가 남아 있어도 다음 실행이 거기 물리지 않는다.
- **승인해 줄 사람이 없는 시간대에 도는 만큼, 쓰기 도구 확인이 걸리면 실행하지 않고** 취소로 정리한 뒤 "확인이 필요해서 안 했다"고만 알린다.

## 점검용 엔드포인트

- `GET /health` — 상태, OpenAI 키 유무
- `GET /memories` — 저장된 장기 기억 최근순
- `POST /voice/stt` — 오디오 → 텍스트
- `POST /voice/tts` — 텍스트 → mp3

## 디렉터리

```
app/
├── main.py            진입점. DB 초기화, 그래프·스케줄러 기동
├── config.py          설정
├── llm.py             OpenAI 클라이언트 팩토리(재시도/타임아웃)
├── agent/             LangGraph (state, nodes, graph, prompts, runtime)
├── tools/             builtin, search, reminders, schedules, calendar, gmail,
│                      code, hue, android, browser, youtube
├── memory/            장기 기억 (pgvector)
├── voice/             STT / TTS
├── api/               ws, rest, voice, hud, notifications, deps(인증)
├── db/                모델, 세션, 감사 로그
└── static/            설치형 PWA 클라이언트 + HUD 화면
scripts/
├── google_auth.py     Google OAuth 1회 인증
└── hue_auth.py        필립스 휴 앱키 1회 발급
tests/                 도구 레지스트리·인증·각 도구 단위 검증 (pytest)
voice_client/          PC 상주 음성 데몬 (웨이크워드·박수·TTS 파이프라인)
```

```bash
pip install -r requirements-dev.txt
pytest
```

## 운영 메모

- **감사 로그**: 모든 도구 실행이 `audit_log` 테이블에 남는다. "안 시킨 메일이 갔다" 같은 상황의 추적 단서.
- **LLM 트레이싱**: 더 깊게 보려면 LangSmith 환경변수(`LANGCHAIN_TRACING_V2` 등)를 주입하면 된다.
- **백업**: 장기 기억이 자비스의 자산이다. Postgres 볼륨(`pg_data`)을 주기적으로 백업할 것.

## 로드맵

- [x] Docker, FastAPI, WebSocket 스트리밍 채팅
- [x] LangGraph 에이전트 (도구 / 반추)
- [x] 장기 기억 (pgvector), 영속 체크포인트
- [x] 위험 작업 확인 절차
- [x] 도구: 캘린더, Gmail, 웹 검색, 리마인더
- [x] 음성 (STT/TTS)
- [x] 능동 알림 (스케줄러)
- [x] 설치형 PWA
- [x] 웨이크워드(openWakeWord) / 박수 깨우기
- [x] Claude Code 연동 (음성 코딩)
- [x] 대화 모드 (답변 뒤 깨우지 않고 이어 말하기) / 운전 모드
- [x] 접속 토큰 인증, 테스트 뼈대
- [x] 도구: 필립스 휴, 안드로이드(ADB), 브라우저 자동화, 유튜브
- [x] 사용자가 걸어 두는 정기 작업
- [ ] 세컨브레인 (문서 인덱싱 RAG)
- [ ] 카메라 (얼굴·사물 인식, 제스처)
- [ ] 원격 접속 (Tailscale)
- [ ] 화자 인식
- [ ] 멀티 디바이스 동기화
