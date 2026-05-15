"""Additive ALTER TABLE steps for databases created before new columns existed."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text


def apply_legacy_column_migrations(engine: object) -> None:
    """Apply idempotent column additions on legacy discovery databases."""
    from sqlalchemy.engine import Engine

    if not isinstance(engine, Engine):
        return
    with engine.begin() as conn:
        _apply_androidtv_friendly_name_migration(conn)
        _apply_androidtv_uuid_model_migration(conn)


def _apply_androidtv_friendly_name_migration(conn: Connection) -> None:
    inspector = inspect(conn)
    if "androidtv_discovered_hosts" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("androidtv_discovered_hosts")}
    if "friendly_name" in cols:
        return
    conn.execute(text("ALTER TABLE androidtv_discovered_hosts ADD COLUMN friendly_name TEXT"))


def _apply_androidtv_uuid_model_migration(conn: Connection) -> None:
    inspector = inspect(conn)
    if "androidtv_discovered_hosts" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("androidtv_discovered_hosts")}
    if "uuid" not in cols:
        conn.execute(text("ALTER TABLE androidtv_discovered_hosts ADD COLUMN uuid TEXT"))
    if "model_name" not in cols:
        conn.execute(text("ALTER TABLE androidtv_discovered_hosts ADD COLUMN model_name TEXT"))
