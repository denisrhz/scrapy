from pathlib import Path

from sqlalchemy import Engine, event, inspect
from sqlmodel import Session, SQLModel, create_engine

from src.config import DEFAULT_DB_PATH, resolve_db_path

# Importing the models is required — without it SQLModel.metadata doesn't
# know about the tables and create_all() creates an empty DB.
from src.models import (  # noqa: F401
    Channel,
    ModelActor,
    Source,
    SourceEntrypoint,
    Tag,
    Video,
    VideoModelActorLink,
    VideoTagLink,
    VideoTranslation,
)

# DB file path and the Engine built from it. The Engine is created lazily:
# the CLI calls set_db_path() from a global callback first, then queries run.
_db_path: Path = DEFAULT_DB_PATH
_engine: Engine | None = None


def set_db_path(db_path: str | Path) -> None:
    """Switches the DB. The previous Engine closes along with its connection pool.

    A bare name (`database.db`) resolves into db/ — see resolve_db_path().
    """
    global _db_path, _engine

    if _engine is not None:
        _engine.dispose()
        _engine = None
    _db_path = resolve_db_path(db_path)


def get_engine() -> Engine:
    """Returns the Engine for the current DB path, creating it on first use.

    Sessions must only be created through this Engine (or via get_session()):
    a wrapper object instead of a real Engine breaks SQLAlchemy's connection
    cache — the session pulls a new connection from the pool on every query,
    and its own open read transactions end up blocking its own INSERTs
    ("database is locked").
    """
    global _engine

    if _engine is None:
        # SQLite doesn't create intermediate directories on its own —
        # without this the first init-db on a fresh clone fails with
        # "unable to open database file".
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        # timeout — how long to wait for a lock to clear at the DBAPI connection level
        _engine = create_engine(
            f"sqlite:///{_db_path}", echo=False, connect_args={"timeout": 30}
        )
        event.listen(_engine, "connect", _apply_sqlite_pragmas)
    return _engine


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """WAL lets reads (e.g. from a GUI client) happen alongside writes."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())
    _migrate_existing_schema()


def _migrate_existing_schema() -> None:
    """Adds new columns to an existing SQLite DB without dropping any data."""
    engine = get_engine()
    columns = {column["name"] for column in inspect(engine).get_columns("video")}
    if "views" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE video ADD COLUMN views INTEGER NOT NULL DEFAULT 0"
            )

    status_column = next(
        column for column in inspect(engine).get_columns("video") if column["name"] == "status"
    )
    if "CHAR" in str(status_column["type"]).upper():
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("DROP TABLE IF EXISTS video_new")
                connection.exec_driver_sql(
                    """
                    CREATE TABLE video_new (
                        id INTEGER NOT NULL PRIMARY KEY,
                        source_id INTEGER,
                        source_entrypoint_id INTEGER,
                        channel_id INTEGER,
                        url VARCHAR NOT NULL UNIQUE,
                        thumb_url VARCHAR NOT NULL,
                        download_url VARCHAR,
                        alt VARCHAR,
                        "desc" VARCHAR,
                        duration INTEGER NOT NULL,
                        status INTEGER NOT NULL DEFAULT 1,
                        error_message VARCHAR,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        views INTEGER NOT NULL DEFAULT 0,
                        FOREIGN KEY(source_id) REFERENCES source(id),
                        FOREIGN KEY(source_entrypoint_id) REFERENCES sourceentrypoint(id),
                        FOREIGN KEY(channel_id) REFERENCES channel(id)
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO video_new (
                        id, source_id, source_entrypoint_id, channel_id, url, thumb_url,
                        download_url, alt, "desc", duration, status, error_message,
                        created_at, updated_at, views
                    )
                    SELECT
                        id, source_id, source_entrypoint_id, channel_id, url, thumb_url,
                        download_url, alt, "desc", duration,
                        CASE status
                            WHEN 'NONE' THEN 0
                            WHEN 'PARSED' THEN 1
                            WHEN 'REWRITTEN' THEN 2
                            WHEN 'TRANSLATED' THEN 4
                            WHEN 'DOWNLOADED' THEN 8
                            WHEN 'EXPORTED' THEN 16
                            WHEN 'FAILED' THEN 32
                            WHEN 'REPARSE' THEN 64
                            ELSE 1
                        END,
                        error_message, created_at, updated_at, views
                    FROM video
                    """
                )
                connection.exec_driver_sql("DROP TABLE video")
                connection.exec_driver_sql("ALTER TABLE video_new RENAME TO video")
                connection.exec_driver_sql("CREATE INDEX ix_video_source_id ON video(source_id)")
                connection.exec_driver_sql(
                    "CREATE INDEX ix_video_source_entrypoint_id ON video(source_entrypoint_id)"
                )
                connection.exec_driver_sql("CREATE INDEX ix_video_channel_id ON video(channel_id)")
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def get_session() -> Session:
    return Session(get_engine())
