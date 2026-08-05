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
    # text-embedding-3 계열은 차원을 줄여서 받을 수 있다(512/1024). 벡터가 작아지는 만큼
    # HNSW 검색·적재와 DB 용량이 가벼워지고, 한국어 짧은 문장 검색 품질은 거의 그대로다.
    # 다만 이 값을 바꾸면 기존 벡터와 섞이지 않는다 — memory_items / document_chunks 를
    # 지우고(DROP TABLE) 다시 띄운 뒤 기억·노트를 새로 색인해야 한다.
    embedding_dim: int = 1536
    # gpt-4o-mini-transcribe 는 whisper-1 보다 응답이 빠르다. 호환 문제가 있으면
    # .env 에서 STT_MODEL=whisper-1 로 되돌릴 수 있다.
    stt_model: str = "gpt-4o-mini-transcribe"
    # "openai"(기본) 또는 "local". local 은 faster-whisper 를 이 기계에서 돌린다 —
    # 네트워크 왕복이 없어 차 안에서 유리하다. 실패 시 자동으로 클라우드로 넘어간다.
    stt_engine: str = "openai"
    stt_local_model: str = "small"  # tiny / base / small / medium / large-v3
    # 로컬 받아쓰기 빔 폭. 한두 문장짜리 명령에는 1 로 충분하다 — 한국어 명령 다섯
    # 문장으로 재 보니 5 와 결과가 글자 하나까지 같았다. 속도도 5% 안쪽이라 사실상
    # 차이가 없으니, 잘 못 알아듣는다 싶으면 올려도 잃을 게 없다.
    # (틀리는 건 대개 '꺼줘→꺼져' 같은 어미인데, 빔 폭이 아니라 모델 크기 문제다.)
    stt_local_beam: int = 1
    tts_model: str = "gpt-4o-mini-tts"
    # 목소리가 인격의 절반이다. onyx 는 낮고 차분해서 자비스 인상에 가깝다.
    # 취향에 따라 ash(단단함) / sage(부드러움) 로 바꾼다.
    tts_voice: str = "onyx"
    # gpt-4o-*-tts 계열에서만 먹는 톤 지시. 같은 목소리도 이 문장으로 인상이 꽤 달라진다.
    tts_instructions: str = (
        "침착하고 절제된 집사의 말투로, 낮고 또렷하게. 과장하거나 호들갑 떨지 말고 "
        "필요한 말만 정확히 전한다. 다만 차갑지는 않게 — 오래 곁을 지킨 사람의 온도로. "
        "자연스러운 한국어 억양을 지킨다."
    )
    # 음성 합성 제공자. openai 는 목소리 복제가 안 된다 — 복제한 목소리를 쓰려면
    # elevenlabs 로 바꾸고 키와 voice id 를 넣는다(키가 없으면 openai 로 되돌아간다).
    tts_provider: str = "openai"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"

    # 외부 검색 (없으면 ddgs 폴백)
    tavily_api_key: str = ""

    # DB
    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis"
    # 한 턴이 커넥션을 여럿 잡는다(도구 동시 실행 + 백그라운드 감사 로그).
    # SQLAlchemy 기본값 5 로는 도구 서너 개짜리 턴에서 대기가 생긴다.
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # 인격 / 로캘
    assistant_name: str = "자비스"
    owner_name: str = "창래"
    timezone: str = "Asia/Seoul"

    # 접속 토큰. 비우면 인증 없음(로컬 단독 실행).
    # 이 기계 밖에서 붙을 거라면 — 테일스케일 같은 사설망 뒤라도 — 반드시 채운다.
    javis_token: str = ""

    # 동작 토글
    use_postgres_checkpointer: bool = True
    # Postgres 체크포인터를 못 쓸 때 물러날 파일. 윈도우 호스트에서 백엔드를 띄우면
    # psycopg 가 붙지 못하는데(이벤트 루프 문제), 여기로 물러나면 재시작해도 대화가 이어진다.
    checkpoint_sqlite_path: str = "credentials/checkpoints.sqlite"
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
    # 전화도 같은 태도. 기본은 다이얼러에 번호만 채우고 통화 버튼은 사람이 누른다.
    # 운전 중처럼 화면을 못 볼 때만 켠다 — 잘못 걸면 되돌릴 수 없다.
    android_call_autodial: bool = False

    # 브라우저 자동화 (Playwright). 로그인 세션을 유지하려고 프로필을 한곳에 둔다.
    browser_user_data_dir: str = "credentials/browser"
    browser_headless: bool = True
    browser_timeout: float = 30.0

    # 유튜브 데이터 API. 콘솔에서 API 키만 발급하면 된다(구글 OAuth 스코프는 안 건드린다).
    youtube_api_key: str = ""

    # 카카오 REST API 키 하나로 장소 검색(Local)과 길찾기(Mobility) 를 같이 쓴다.
    kakao_rest_api_key: str = ""
    # 출발지를 안 밝혔을 때의 기본값. "집에서 공항까지" 같은 말이 되게 한다.
    home_address: str = ""

    # 카메라. 얼굴 임베딩 차원은 데몬이 쓰는 모델에 맞춘다 — OpenCV SFace 는 128.
    # 이 값을 바꾸면 known_faces 테이블을 지우고 다시 등록해야 한다.
    face_embedding_dim: int = 128
    # 사물·장면 설명에 쓸 모델. 비우면 llm_model 을 그대로 쓴다(gpt-4o 계열은 이미지 입력 지원).
    vision_model: str = ""

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
