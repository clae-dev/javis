from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings


class Base(DeclarativeBase):
    pass


class MemoryItem(Base):
    """장기 기억 한 조각. 대화 끝 반추 단계에서 추려서 저장된다."""

    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), default="general")
    importance: Mapped[int] = mapped_column(Integer, default=5)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reminder(Base):
    """개인 리마인더. due_at 이 지나면 스케줄러가 알림을 보낸다."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    """색인한 노트 파일 하나.

    mtime 이 그대로면 파일을 열지도 않고 건너뛴다. mtime 만 바뀌고 내용이 같으면
    (편집기가 저장만 다시 한 경우) sha 가 같으므로 임베딩을 다시 만들지 않는다.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 노트 폴더 기준 상대 경로. 폴더를 통째로 옮겨도 재색인이 필요 없다.
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    sha: Mapped[str] = mapped_column(String(64))
    mtime: Mapped[float] = mapped_column(Float)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentChunk(Base):
    """노트를 헤딩 단위로 쪼갠 조각.

    조각마다 어느 문단인지(heading) 를 함께 실어 둔다. 검색으로 조각 하나만 건져
    올렸을 때 앞뒤 맥락 없이도 무슨 얘기인지 알 수 있어야 하기 때문이다.
    """

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    heading: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))


class KnownFace(Base):
    """자비스가 알아보는 얼굴.

    임베딩 계산과 매칭은 카메라가 달린 기계(비전 데몬)에서 한다. 서버는 보관하고
    나눠 주기만 한다 — 아이 얼굴 같은 민감정보를 클라우드로 보내지 않기 위해서다.
    """

    __tablename__ = "known_faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    relation: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.face_embedding_dim))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduledJob(Base):
    """사용자가 말로 걸어 둔 정기 작업.

    cron 이 되면 prompt 를 자비스에게 그대로 물어보고 답을 알림으로 띄운다.
    리마인더가 '정해 둔 문장을 그때 알려주는' 것이라면, 이쪽은 '그때 가서 실제로
    알아보고 알려주는' 것이다.
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    cron: Mapped[str] = mapped_column(String(64))  # 표준 5필드 (분 시 일 월 요일)
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """LLM 호출·도구 실행 기록. 자비스가 이상하게 굴 때 추적할 유일한 단서."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_kind_ok", "kind", "ok"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # llm | tool
    name: Mapped[str] = mapped_column(String(128))
    request: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserProfile(Base):
    """사용자에 대해 자비스가 쌓아 올린 한 장짜리 프로필 (싱글톤, id=1).

    반추 단계에서 새 사실이 나올 때마다 갱신되고, 매 응답의 시스템 프롬프트에 주입된다.
    이게 '나를 알아가는' 느낌의 뼈대다.
    """

    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
