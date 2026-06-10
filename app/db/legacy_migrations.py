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
        _apply_mytracks_pairing_columns_migration(conn)


def _apply_mytracks_pairing_columns_migration(conn: Connection) -> None:
    inspector = inspect(conn)
    if "mytracks_settings" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("mytracks_settings")}
    additions: list[tuple[str, str]] = [
        ("domesti_public_base_url", "TEXT"),
        ("last_pair_error", "TEXT"),
        ("last_verify_at", "REAL"),
        ("last_verify_ok", "INTEGER"),
        ("location_history_max_age_s", "REAL"),
        ("location_history_min_keep_count", "INTEGER"),
        ("location_history_unlimited", "INTEGER NOT NULL DEFAULT 0"),
        ("location_updates_accepted", "INTEGER NOT NULL DEFAULT 1"),
        ("paired_at", "REAL"),
        ("participant_location_test_url", "TEXT"),
        ("participant_location_update_url", "TEXT"),
    ]
    for name, sql_type in additions:
        if name not in cols:
            conn.execute(text(f"ALTER TABLE mytracks_settings ADD COLUMN {name} {sql_type}"))


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
