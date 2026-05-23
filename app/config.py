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
    stt_model: str = "whisper-1"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"

    # 외부 검색 (없으면 ddgs 폴백)
    tavily_api_key: str = ""

    # DB
    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis"

    # 인격 / 로캘
    assistant_name: str = "자비스"
    owner_name: str = "창래"
    timezone: str = "Asia/Seoul"

    # 동작 토글
    use_postgres_checkpointer: bool = True
    enable_scheduler: bool = True

    # Google
    google_credentials_path: str = "credentials/google.json"
    google_token_path: str = "credentials/token.json"

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
