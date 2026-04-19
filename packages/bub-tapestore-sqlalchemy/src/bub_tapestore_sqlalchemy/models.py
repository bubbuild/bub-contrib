from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, DateTime


class Base(DeclarativeBase):
    pass


class TapeRecord(Base):
    __tablename__ = "tapes"
    __table_args__ = (
        Index("uq_tapes_name_key", "name_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(String(64), nullable=False)
    last_entry_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TapeEntryRecord(Base):
    __tablename__ = "tape_entries"
    __table_args__ = (
        Index("idx_tape_entries_kind", "tape_id", "kind", "entry_id"),
        Index("idx_tape_entries_anchor_name_key", "tape_id", "anchor_name_key", "entry_id"),
    )

    tape_id: Mapped[int] = mapped_column(
        ForeignKey("tapes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_name_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    meta: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    entry_date: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
