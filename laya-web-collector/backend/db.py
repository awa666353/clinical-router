"""Database engine and session helpers.

W2 of the Supabase migration:

* Postgres uses an explicit pool (Supavisor session pooler, port 5432).
* Transaction pooler URLs (port 6543) disable psycopg prepared statements,
  because PgBouncer in transaction mode cannot support them.
* ``create_all`` only runs when ``DB_AUTO_CREATE=1``; production relies on
  ``supabase/migrations/`` instead of a second schema source.
"""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _uses_transaction_pooler(url: str) -> bool:
    if _is_sqlite(url):
        return False
    try:
        return urlsplit(url).port == 6543
    except ValueError:  # malformed URL: let create_engine produce the real error
        return False


def _make_engine():
    connect_args: dict = {}
    engine_kwargs: dict = {"future": True}
    url = settings.database_url
    if _is_sqlite(url):
        connect_args = {"check_same_thread": False}
    else:
        engine_kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        if _uses_transaction_pooler(url):
            # psycopg 3: disable server-side prepared statements for PgBouncer
            # transaction pooling.  The default deployment uses the session
            # pooler, so this branch is normally unused.
            connect_args = {"prepare_threshold": None}
    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)
    if _is_sqlite(url):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def init_db() -> None:
    """Create tables only for local/dev SQLite (or an explicit opt-in).

    Production Postgres must be built from ``supabase/migrations/``; running
    ``create_all`` there would create an unmanaged second schema and race two
    uvicorn workers (B8).
    """
    if settings.db_auto_create:
        Base.metadata.create_all(engine)
        if engine.dialect.name == "sqlite":
            # create_all does not add columns to an existing local game table.
            # Keep previously collected synthetic sessions usable after updates.
            columns = {column["name"] for column in inspect(engine).get_columns("game_score_events")}
            additions = {
                "note_text": "TEXT",
                "note_llm_call_id": "TEXT",
                "note_status": "VARCHAR(24) NOT NULL DEFAULT 'queued'",
                "note_attempts": "INTEGER NOT NULL DEFAULT 0",
                "note_started_at": "DATETIME",
            }
            with engine.begin() as connection:
                for name, definition in additions.items():
                    if name not in columns:
                        connection.exec_driver_sql(f"ALTER TABLE game_score_events ADD COLUMN {name} {definition}")


def current_schema() -> str | None:
    with engine.connect() as connection:
        return connection.execute(text("SELECT current_schema()")).scalar()


def check_database_schema() -> None:
    """Production startup guard: the app must run in ``DB_EXPECTED_SCHEMA``."""
    if settings.app_env != "production":
        return
    schema = current_schema()
    if schema != settings.db_expected_schema:
        raise RuntimeError(
            "refusing to start: database current_schema() is "
            f"{schema!r}, expected {settings.db_expected_schema!r}"
        )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
