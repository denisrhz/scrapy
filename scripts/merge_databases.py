"""Merges several project databases into one new database.

Duplicates are removed by natural keys, not by id: videos by `url`,
tags/models/channels by `name`, sources by `domain`, entrypoints by the pair
(domain, path). The databases' own ids overlap, so all copying goes through
temporary old_id -> new_id mapping tables.

Everything runs in SQL via ATTACH: there's no reason to shuffle 160 thousand
videos and 2 million links row by row in Python.

Usage:
    uv run python -m scripts.merge_databases \\
        --into gay_all_database gay_database asian_gay_channels_database
"""

from __future__ import annotations

import argparse
import sqlite3

from src.config import resolve_db_path
from src.database.session import get_engine, init_db, set_db_path

TABLES = (
    "source",
    "sourceentrypoint",
    "channel",
    "tag",
    "modelactor",
    "video",
    "videotaglink",
    "videomodelactorlink",
    "videotranslation",
)

# Name indexes are only needed during the merge: without them joins by name
# turn into a full scan of 33 thousand tags for every row.
# The schema goes on the index name, not the table: `CREATE INDEX main.ix ON tag(...)`.
MERGE_INDEXES = (
    ("ix_merge_tag_name", "tag(name)"),
    ("ix_merge_channel_name", "channel(name)"),
    ("ix_merge_model_name", "modelactor(name)"),
    ("ix_merge_entrypoint_path", "sourceentrypoint(source_id, path)"),
)


def counts(connection: sqlite3.Connection, schema: str = "main") -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()[0]
        for table in TABLES
    }


def merge_one(connection: sqlite3.Connection, source_path: str) -> None:
    """Adds the contents of one database into the already-open target."""
    connection.execute("ATTACH DATABASE ? AS src", (source_path,))
    try:
        # --- reference data: first, whatever videos point to ----------------
        connection.execute("""
            INSERT INTO main.source (domain, code, is_active, created_at)
            SELECT s.domain, s.code, s.is_active, s.created_at FROM src.source s
            WHERE s.domain NOT IN (SELECT domain FROM main.source)
              AND s.code NOT IN (SELECT code FROM main.source)
        """)
        for table, column in (("channel", "name"), ("tag", "name"), ("modelactor", "name")):
            columns = [
                row[1]
                for row in connection.execute(f"PRAGMA src.table_info({table})")
                if row[1] != "id"
            ]
            listed = ", ".join(columns)
            connection.execute(f"""
                INSERT INTO main.{table} ({listed})
                SELECT {listed} FROM src.{table} s
                WHERE s.{column} NOT IN (SELECT {column} FROM main.{table})
            """)

        connection.execute("""
            CREATE TEMP TABLE map_source AS
            SELECT s.id AS old, m.id AS new
            FROM src.source s JOIN main.source m ON m.domain = s.domain
        """)
        connection.execute("""
            INSERT INTO main.sourceentrypoint
                (source_id, path, is_active, last_parsed_page, total_pages,
                 is_completed, last_parsed_at)
            SELECT ms.new, e.path, e.is_active, e.last_parsed_page, e.total_pages,
                   e.is_completed, e.last_parsed_at
            FROM src.sourceentrypoint e
            JOIN map_source ms ON ms.old = e.source_id
            WHERE NOT EXISTS (
                SELECT 1 FROM main.sourceentrypoint me
                WHERE me.source_id = ms.new AND me.path = e.path
            )
        """)

        # --- mapping tables -------------------------------------------
        for name, statement in (
            ("map_entrypoint", """
                SELECT e.id AS old, me.id AS new
                FROM src.sourceentrypoint e
                JOIN map_source ms ON ms.old = e.source_id
                JOIN main.sourceentrypoint me ON me.source_id = ms.new AND me.path = e.path
            """),
            ("map_channel", """
                SELECT s.id AS old, m.id AS new
                FROM src.channel s JOIN main.channel m ON m.name = s.name
            """),
            ("map_tag", """
                SELECT s.id AS old, m.id AS new
                FROM src.tag s JOIN main.tag m ON m.name = s.name
            """),
            ("map_model", """
                SELECT s.id AS old, m.id AS new
                FROM src.modelactor s JOIN main.modelactor m ON m.name = s.name
            """),
        ):
            connection.execute(f"CREATE TEMP TABLE {name} AS {statement}")
            connection.execute(f"CREATE UNIQUE INDEX ix_{name} ON {name}(old)")

        # --- videos ----------------------------------------------------------
        connection.execute("""
            INSERT INTO main.video
                (source_id, source_entrypoint_id, channel_id, url, thumb_url,
                 download_url, alt, "desc", duration, status, error_message,
                 created_at, updated_at, views)
            SELECT ms.new, me.new, mc.new, v.url, v.thumb_url,
                   v.download_url, v.alt, v."desc", v.duration, v.status, v.error_message,
                   v.created_at, v.updated_at, v.views
            FROM src.video v
            LEFT JOIN map_source ms ON ms.old = v.source_id
            LEFT JOIN map_entrypoint me ON me.old = v.source_entrypoint_id
            LEFT JOIN map_channel mc ON mc.old = v.channel_id
            WHERE v.url NOT IN (SELECT url FROM main.video)
        """)
        connection.execute("""
            CREATE TEMP TABLE map_video AS
            SELECT v.id AS old, m.id AS new
            FROM src.video v JOIN main.video m ON m.url = v.url
        """)
        connection.execute("CREATE UNIQUE INDEX ix_map_video ON map_video(old)")

        # --- links and translations -----------------------------------------------
        connection.execute("""
            INSERT OR IGNORE INTO main.videotaglink (video_id, tag_id)
            SELECT mv.new, mt.new FROM src.videotaglink l
            JOIN map_video mv ON mv.old = l.video_id
            JOIN map_tag mt ON mt.old = l.tag_id
        """)
        connection.execute("""
            INSERT OR IGNORE INTO main.videomodelactorlink (video_id, model_actor_id)
            SELECT mv.new, mm.new FROM src.videomodelactorlink l
            JOIN map_video mv ON mv.old = l.video_id
            JOIN map_model mm ON mm.old = l.model_actor_id
        """)
        connection.execute("""
            INSERT OR IGNORE INTO main.videotranslation
                (video_id, language, title, description, slug, provider, created_at, updated_at)
            SELECT mv.new, t.language, t.title, t.description, t.slug, t.provider,
                   t.created_at, t.updated_at
            FROM src.videotranslation t
            JOIN map_video mv ON mv.old = t.video_id
        """)
        connection.commit()
    finally:
        for name in ("map_source", "map_entrypoint", "map_channel", "map_tag",
                     "map_model", "map_video"):
            connection.execute(f"DROP TABLE IF EXISTS {name}")
        connection.execute("DETACH DATABASE src")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="Source databases (name from db/ or a path)")
    parser.add_argument("--into", required=True, help="New database (name from db/ or a path)")
    args = parser.parse_args()

    target_path = resolve_db_path(args.into)
    if target_path.exists():
        raise SystemExit(f"{target_path} already exists — delete it or pick another name")
    source_paths = [resolve_db_path(name) for name in args.sources]
    for path in source_paths:
        if not path.exists():
            raise SystemExit(f"no such database: {path}")

    # The app itself creates the schema, so the new DB matches what init-db produces.
    set_db_path(target_path)
    init_db()
    get_engine().dispose()

    connection = sqlite3.connect(target_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA journal_mode=WAL")
        for name, target in MERGE_INDEXES:
            connection.execute(f"CREATE INDEX IF NOT EXISTS main.{name} ON {target}")

        for path in source_paths:
            before = counts(connection)
            merge_one(connection, str(path))
            after = counts(connection)
            print(f"\n+ {path.name}")
            for table in TABLES:
                added = after[table] - before[table]
                print(f"    {table:22s} +{added:<9d} total {after[table]}")

        for name, _ in MERGE_INDEXES:
            connection.execute(f"DROP INDEX IF EXISTS main.{name}")
        connection.execute("PRAGMA foreign_key_check")
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        print(f"\nforeign_key_check: {'ok' if not problems else problems[:5]}")
        print("integrity_check:", connection.execute("PRAGMA integrity_check").fetchone()[0])
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    print(f"\ndone: {target_path}")


if __name__ == "__main__":
    main()
