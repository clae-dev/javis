import os

from pydantic_settings import BaseSettings, SettingsConfigDict

# Google API 스코프 — 캘린더 + Gmail(읽기/전송).
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenAI
    openai_api_key: str = ""
    llm_model: str = "gpt-4o"
    fast_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    # gpt-4o-mini-transcribe 는 whisper-1 보다 응답이 빠르다. 호환 문제가 있으면
    # .env 에서 STT_MODEL=whisper-1 로 되돌릴 수 있다.
    stt_model: str = "gpt-4o-mini-transcribe"
    # "openai"(기본) 또는 "local". local 은 faster-whisper 를 이 기계에서 돌린다 —
    # 네트워크 왕복이 없어 차 안에서 유리하다. 실패 시 자동으로 클라우드로 넘어간다.
    stt_engine: str = "openai"
    stt_local_model: str = "small"  # tiny / base / small / medium / large-v3
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    # gpt-4o-*-tts 계열에서만 먹는 톤 지시. 음성에 감정을 싣는다.
    tts_instructions: str = "따뜻하고 다정한 친구 같은 말투로, 자연스러운 한국어 억양과 감정을 담아 말해줘."

    # 외부 검색 (없으면 ddgs 폴백)
    tavily_api_key: str = ""

    # DB
    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis"

    # 인격 / 로캘
    assistant_name: str = "자비스"
    owner_name: str = "창래"
    timezone: str = "Asia/Seoul"

    # 접속 토큰. 비우면 인증 없음(로컬 단독 실행).
    # 이 기계 밖에서 붙을 거라면 — 테일스케일 같은 사설망 뒤라도 — 반드시 채운다.
    javis_token: str = ""

    # 동작 토글
    use_postgres_checkpointer: bool = True
    enable_scheduler: bool = True

    # Google
    google_credentials_path: str = "credentials/google.json"
    google_token_path: str = "credentials/token.json"

    # 필립스 휴. 브리지 IP 를 비우면 discovery.meethue.com 으로 찾는다.
    # 앱키는 `python scripts/hue_auth.py` 로 1회 발급한다.
    hue_bridge_ip: str = ""
    hue_app_key: str = ""

    # 안드로이드 (ADB). adb 가 PATH 에 없으면 실행 파일 경로를 직접 적는다.
    # 기기가 여럿이면 android_serial 로 하나를 고정한다(`adb devices` 로 확인).
    android_adb_path: str = ""
    android_serial: str = ""
    # 문자를 '작성만' 할지 '보내기까지' 할지. 자동 전송은 메시지 앱 화면 구성에 기대는
    # 방식이라 기기에 따라 안 먹을 수 있다. 기본은 작성까지만.
    android_sms_autosend: bool = False

    # 브라우저 자동화 (Playwright). 로그인 세션을 유지하려고 프로필을 한곳에 둔다.
    browser_user_data_dir: str = "credentials/browser"
    browser_headless: bool = True
    browser_timeout: float = 30.0

    # 유튜브 데이터 API. 콘솔에서 API 키만 발급하면 된다(구글 OAuth 스코프는 안 건드린다).
    youtube_api_key: str = ""

    # 세컨브레인 — 색인할 노트 폴더 (옵시디언 vault 든 그냥 폴더든).
    # 비우면 색인·검색 도구가 안내 문구만 돌려주고 나머지는 정상 동작한다.
    notes_path: str = ""
    notes_extensions: str = ".md,.txt,.pdf"
    notes_index_minutes: int = 10  # 증분 색인 주기
    notes_chunk_chars: int = 1200  # 조각 하나의 최대 길이

    # 도구가 남기는 캡처물(폰 스크린샷 등) 보관 위치.
    captures_path: str = "captures"
    # 노트를 읽어 만든 음성 파일 보관 위치.
    podcasts_path: str = "podcasts"

    # Claude Code 연동 (음성 코딩). 여기 등록한 프로젝트만 열 수 있다.
    # 형식: "이름=경로,이름=경로"  예) javis=C:\workspace\Javis
    code_projects: str = ""
    # claude CLI 에 넘길 모델. 별칭(opus/sonnet/haiku)이나 전체 이름.
    # 응답이 느리면 sonnet 으로 낮춘다.
    code_model: str = "opus"
    code_sessions_path: str = "credentials/code_sessions.json"

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def psycopg_dsn(self) -> str:
        """AsyncPostgresSaver(psycopg)용 DSN. SQLAlchemy 드라이버 접미사를 제거한다."""
        return self.database_url.replace("+asyncpg", "").replace("+psycopg", "")


settings = Settings()

# langchain/openai 클라이언트는 OPENAI_API_KEY 환경변수를 직접 읽는다.
# 로컬에서 .env 만 있고 export 안 한 경우를 대비해 보장해 둔다.
if settings.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.tavily_api_key and not os.environ.get("TAVILY_API_KEY"):
    os.environ["TAVILY_API_KEY"] = settings.tavily_api_key
