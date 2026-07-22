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
- **음성**: 마이크로 말하면 Whisper로 받아쓰고, 응답을 TTS로 읽어준다. 받아쓰기는 로컬로도 돌릴 수 있다.
- **능동 알림**: 마감된 리마인더, 아침 일정 브리핑을 먼저 알려준다.
- **PWA**: 휴대폰/데스크톱에 설치 가능. 브라우저 알림 지원.

## 구성

```
브라우저 / 설치형 PWA
        │  WebSocket (텍스트 스트리밍, 능동 알림)  +  REST (음성 STT/TTS)
   FastAPI Gateway
        │
   LangGraph Agent ── PostgreSQL + pgvector (장기 기억 / 대화 체크포인트)
        │
   Tools: 시간, 웹검색, 기억, 리마인더, 캘린더, Gmail, 코드(Claude Code CLI)
        ↑
   APScheduler (능동 알림)
```

흐름은 **의도 분류 → (필요하면) 기억 조회 → 에이전트 → 도구 실행 → 반추(reflect)**. 잡담은 기억 조회를 건너뛰어 비용·지연을 아끼고, 도구가 필요하면 ReAct 루프를 돈다. 쓰기 작업은 LangGraph `interrupt`로 그래프 흐름 안에서 확인을 받는다.

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
| `CODE_PROJECTS` | 음성으로 다룰 코드 프로젝트 `이름=경로` 목록 | (빈값) |
| `CODE_MODEL` | 코드 작업에 쓸 Claude 모델 | `opus` |
| `DATABASE_URL` | Postgres 접속 문자열 | compose 기본값 |
| `TIMEZONE` | 스케줄러·시간 표시 기준 | `Asia/Seoul` |
| `USE_POSTGRES_CHECKPOINTER` | 대화 영속화 | `true` |
| `ENABLE_SCHEDULER` | 능동 알림 | `true` |
| `OWNER_NAME` / `ASSISTANT_NAME` | 사용자·비서 이름 | `창래` / `자비스` |

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
├── tools/             builtin, search, reminders, calendar, gmail, code
├── memory/            장기 기억 (pgvector)
├── voice/             STT / TTS
├── api/               ws, rest, voice, notifications
├── db/                모델, 세션, 감사 로그
└── static/            설치형 PWA 클라이언트
scripts/
└── google_auth.py     Google OAuth 1회 인증
```

## 운영 메모

- **감사 로그**: 모든 도구 실행이 `audit_log` 테이블에 남는다. "안 시킨 메일이 갔다" 같은 상황의 추적 단서.
- **LLM 트레이싱**: 더 깊게 보려면 LangSmith 환경변수(`LANGCHAIN_TRACING_V2` 등)를 주입하면 된다.
- **백업**: 장기 기억이 자비스의 자산이다. Postgres 볼륨(`pg_data`)을 주기적으로 백업할 것.

## 로드맵

- [x] Docker, FastAPI, WebSocket 스트리밍 채팅
- [x] LangGraph 에이전트 (의도 분류 / 도구 / 반추)
- [x] 장기 기억 (pgvector), 영속 체크포인트
- [x] 위험 작업 확인 절차
- [x] 도구: 캘린더, Gmail, 웹 검색, 리마인더
- [x] 음성 (STT/TTS)
- [x] 능동 알림 (스케줄러)
- [x] 설치형 PWA
- [x] 웨이크워드(openWakeWord) / 박수 깨우기
- [x] Claude Code 연동 (음성 코딩)
- [x] 대화 모드 (답변 뒤 깨우지 않고 이어 말하기) / 운전 모드
- [ ] 화자 인식
- [ ] 멀티 디바이스 동기화
