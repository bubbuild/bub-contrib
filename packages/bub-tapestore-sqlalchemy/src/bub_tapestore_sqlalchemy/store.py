from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable

from republic import TapeEntry, TapeQuery
from republic.core.errors import ErrorKind
from republic.core.results import ErrorPayload
from sqlalchemy import Engine, create_engine, event, inspect, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bub_tapestore_sqlalchemy.models import Base, TapeEntryRecord, TapeRecord


class SQLAlchemyTapeStore:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._url = self._normalize_url(url)
        self._echo = echo
        self._write_lock = threading.RLock()
        self._engine = create_engine(
            self._url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            connect_args=self._connect_args(self._url),
        )
        self._configure_engine(self._engine)
        self._session_factory = sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=Session,
        )
        Base.metadata.create_all(self._engine)
        self._validate_schema()

    def list_tapes(self) -> list[str]:
        with self._session_factory() as session:
            return list(
                session.scalars(select(TapeRecord.name).order_by(TapeRecord.name)).all()
            )

    def reset(self, tape: str) -> None:
        with self._write_lock:
            with self._session_factory.begin() as session:
                tape_record = self._find_tape_record(session, tape)
                if tape_record is not None:
                    session.delete(tape_record)

    def append(self, tape: str, entry: TapeEntry) -> None:
        with self._write_lock:
            with self._session_factory.begin() as session:
                tape_record = self._load_or_create_tape(session, tape)
                next_entry_id = self._next_entry_id(session, tape_record)
                anchor_name = self._anchor_name_of(entry)
                session.add(
                    TapeEntryRecord(
                        tape_id=tape_record.id,
                        entry_id=next_entry_id,
                        kind=entry.kind,
                        anchor_name=anchor_name,
                        anchor_name_key=self._key_for(anchor_name) if anchor_name else None,
                        payload=dict(entry.payload),
                        meta=dict(entry.meta),
                        entry_date=entry.date,
                    )
                )

    def fetch_all(self, query: TapeQuery[SQLAlchemyTapeStore]) -> Iterable[TapeEntry]:
        with self._session_factory() as session:
            tape_id = self._tape_id(session, query.tape)
            if tape_id is None:
                self._raise_missing_for_query(query)
                return []

            lower_bound, upper_bound = self._resolve_bounds(
                session=session,
                tape_id=tape_id,
                query=query,
            )
            statement = select(TapeEntryRecord).where(TapeEntryRecord.tape_id == tape_id)
            if lower_bound is not None:
                statement = statement.where(TapeEntryRecord.entry_id > lower_bound)
            if upper_bound is not None:
                statement = statement.where(TapeEntryRecord.entry_id < upper_bound)
            kinds = self._normalized_kinds(query._kinds)
            if kinds:
                statement = statement.where(TapeEntryRecord.kind.in_(kinds))
            statement = statement.order_by(TapeEntryRecord.entry_id)
            if query._limit is not None:
                statement = statement.limit(query._limit)
            records = session.scalars(statement).all()
        return [self._entry_from_record(record) for record in records]

    @staticmethod
    def entry_from_payload(payload: object) -> TapeEntry | None:
        if not isinstance(payload, dict):
            return None
        entry_id = payload.get("id")
        kind = payload.get("kind")
        entry_payload = payload.get("payload")
        meta = payload.get("meta")
        date = payload.get("date")
        if not isinstance(entry_id, int):
            return None
        if not isinstance(kind, str):
            return None
        if not isinstance(entry_payload, dict):
            return None
        if not isinstance(meta, dict):
            meta = {}
        if not isinstance(date, str):
            return None
        return TapeEntry(
            id=entry_id,
            kind=kind,
            payload=dict(entry_payload),
            meta=dict(meta),
            date=date,
        )

    @staticmethod
    def _normalize_url(url: str) -> URL:
        try:
            return make_url(url)
        except ArgumentError as exc:
            raise ValueError(f"Invalid SQLAlchemy URL: {url}") from exc

    @staticmethod
    def _connect_args(url: URL) -> dict[str, object]:
        if url.get_backend_name() == "sqlite":
            return {
                "check_same_thread": False,
                "timeout": 30,
            }
        return {}

    @staticmethod
    def _configure_engine(engine: Engine) -> None:
        if engine.url.get_backend_name() != "sqlite":
            return

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.close()

    def _validate_schema(self) -> None:
        inspector = inspect(self._engine)
        table_names = set(inspector.get_table_names())
        if "tapes" not in table_names or "tape_entries" not in table_names:
            raise RuntimeError("SQLAlchemy tape store schema is incomplete.")
        tape_columns = {column["name"] for column in inspector.get_columns("tapes")}
        entry_columns = {column["name"] for column in inspector.get_columns("tape_entries")}
        required_tape_columns = {"id", "name", "name_key", "last_entry_id", "created_at"}
        required_entry_columns = {
            "tape_id",
            "entry_id",
            "kind",
            "anchor_name",
            "anchor_name_key",
            "payload",
            "meta",
            "entry_date",
            "created_at",
        }
        if not required_tape_columns.issubset(tape_columns):
            raise RuntimeError(
                "Existing tapes table uses an incompatible schema. Delete the old database and recreate it."
            )
        if not required_entry_columns.issubset(entry_columns):
            raise RuntimeError(
                "Existing tape_entries table uses an incompatible schema. Delete the old database and recreate it."
            )
        for index in TapeRecord.__table__.indexes | TapeEntryRecord.__table__.indexes:
            index.create(bind=self._engine, checkfirst=True)

    @staticmethod
    def _anchor_name_of(entry: TapeEntry) -> str | None:
        if entry.kind != "anchor":
            return None
        name = entry.payload.get("name")
        if isinstance(name, str) and name:
            return name
        return None

    @staticmethod
    def _entry_from_record(record: TapeEntryRecord) -> TapeEntry:
        payload = record.payload if isinstance(record.payload, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        return TapeEntry(
            id=record.entry_id,
            kind=record.kind,
            payload=dict(payload),
            meta=dict(meta),
            date=record.entry_date,
        )

    @staticmethod
    def _key_for(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalized_kinds(kinds: tuple[object, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for kind in kinds:
            if isinstance(kind, str):
                normalized.append(kind)
                continue
            if isinstance(kind, Iterable):
                for nested_kind in kind:
                    if not isinstance(nested_kind, str):
                        raise TypeError("TapeQuery.kinds() entries must be strings.")
                    normalized.append(nested_kind)
                continue
            raise TypeError("TapeQuery.kinds() entries must be strings.")
        return tuple(normalized)

    @classmethod
    def _load_or_create_tape(cls, session: Session, tape: str) -> TapeRecord:
        name_key = cls._key_for(tape)
        tape_record = cls._find_tape_record(session, tape, for_update=True)
        if tape_record is not None:
            return tape_record
        try:
            with session.begin_nested():
                tape_record = TapeRecord(name=tape, name_key=name_key, last_entry_id=0)
                session.add(tape_record)
                session.flush()
        except IntegrityError:
            pass
        tape_record = cls._find_tape_record(session, tape, for_update=True)
        if tape_record is None:
            raise RuntimeError(f"Failed to load tape record for '{tape}'.")
        return tape_record

    @classmethod
    def _tape_id(cls, session: Session, tape: str) -> int | None:
        tape_record = cls._find_tape_record(session, tape)
        return tape_record.id if tape_record is not None else None

    @classmethod
    def _find_tape_record(
        cls,
        session: Session,
        tape: str,
        *,
        for_update: bool = False,
    ) -> TapeRecord | None:
        statement = select(TapeRecord).where(
            TapeRecord.name_key == cls._key_for(tape),
            TapeRecord.name == tape,
        )
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    @classmethod
    def _next_entry_id(cls, session: Session, tape_record: TapeRecord) -> int:
        current_entry_id = tape_record.last_entry_id
        while True:
            next_entry_id = current_entry_id + 1
            result = session.execute(
                update(TapeRecord)
                .where(
                    TapeRecord.id == tape_record.id,
                    TapeRecord.last_entry_id == current_entry_id,
                )
                .values(last_entry_id=next_entry_id)
            )
            if result.rowcount == 1:
                tape_record.last_entry_id = next_entry_id
                return next_entry_id
            current_entry_id = session.scalar(
                select(TapeRecord.last_entry_id)
                .where(TapeRecord.id == tape_record.id)
                .with_for_update()
            )
            if current_entry_id is None:
                raise RuntimeError(f"Failed to allocate entry id for tape '{tape_record.name}'.")

    def _resolve_bounds(
        self,
        *,
        session: Session,
        tape_id: int,
        query: TapeQuery[SQLAlchemyTapeStore],
    ) -> tuple[int | None, int | None]:
        if query._between is not None:
            start_name, end_name = query._between
            start_id = self._find_anchor_id(
                session=session,
                tape_id=tape_id,
                name=start_name,
                forward=False,
            )
            if start_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found."
                )
            end_id = self._find_anchor_id(
                session=session,
                tape_id=tape_id,
                name=end_name,
                forward=True,
                after_entry_id=start_id,
            )
            if end_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND, f"Anchor '{end_name}' was not found."
                )
            return start_id, end_id

        if query._after_last:
            anchor_id = self._find_anchor_id(
                session=session,
                tape_id=tape_id,
                name=None,
                forward=False,
            )
            if anchor_id is None:
                raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
            return anchor_id, None

        if query._after_anchor is not None:
            anchor_id = self._find_anchor_id(
                session=session,
                tape_id=tape_id,
                name=query._after_anchor,
                forward=False,
            )
            if anchor_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND,
                    f"Anchor '{query._after_anchor}' was not found.",
                )
            return anchor_id, None

        return None, None

    @staticmethod
    def _find_anchor_id(
        *,
        session: Session,
        tape_id: int,
        name: str | None,
        forward: bool,
        after_entry_id: int = 0,
    ) -> int | None:
        statement = select(TapeEntryRecord.entry_id).where(
            TapeEntryRecord.tape_id == tape_id,
            TapeEntryRecord.kind == "anchor",
        )
        if name is not None:
            statement = statement.where(
                TapeEntryRecord.anchor_name_key == SQLAlchemyTapeStore._key_for(name),
                TapeEntryRecord.anchor_name == name,
            )
        if after_entry_id > 0:
            statement = statement.where(TapeEntryRecord.entry_id > after_entry_id)
        order_column = TapeEntryRecord.entry_id.asc() if forward else TapeEntryRecord.entry_id.desc()
        statement = statement.order_by(order_column).limit(1)
        return session.scalar(statement)

    @staticmethod
    def _raise_missing_for_query(query: TapeQuery[SQLAlchemyTapeStore]) -> None:
        if query._between is not None:
            start_name, _ = query._between
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found."
            )
        if query._after_last:
            raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
        if query._after_anchor is not None:
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{query._after_anchor}' was not found."
            )
