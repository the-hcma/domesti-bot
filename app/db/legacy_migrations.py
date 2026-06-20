"""Additive ALTER TABLE steps for databases created before new columns existed."""

from __future__ import annotations

import time

from sqlalchemy import Connection, inspect, text

from app.user_names import default_display_name, parse_person_name


def apply_legacy_column_migrations(engine: object) -> None:
    """Apply idempotent column additions on legacy discovery databases."""
    from sqlalchemy.engine import Engine

    if not isinstance(engine, Engine):
        return
    with engine.begin() as conn:
        _apply_androidtv_friendly_name_migration(conn)
        _apply_androidtv_uuid_model_migration(conn)
        _apply_mytracks_pairing_columns_migration(conn)
        _apply_mytracks_user_nomenclature_migration(conn)
        _apply_rule_user_geofence_state_last_location_migration(conn)
        _apply_rule_user_location_connection_type_migration(conn)
        _apply_rule_user_tables_migration(conn)


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


def _apply_mytracks_user_nomenclature_migration(conn: Connection) -> None:
    inspector = inspect(conn)
    if "mytracks_settings" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("mytracks_settings")}
    additions: list[tuple[str, str]] = [
        ("last_users_sync_at", "REAL"),
        ("user_location_test_url", "TEXT"),
        ("user_location_update_url", "TEXT"),
    ]
    for name, sql_type in additions:
        if name not in cols:
            conn.execute(text(f"ALTER TABLE mytracks_settings ADD COLUMN {name} {sql_type}"))
    cols = {c["name"] for c in inspector.get_columns("mytracks_settings")}
    if "last_users_sync_at" in cols and "last_participants_sync_at" in cols:
        conn.execute(
            text(
                "UPDATE mytracks_settings SET last_users_sync_at = last_participants_sync_at "
                "WHERE last_users_sync_at IS NULL AND last_participants_sync_at IS NOT NULL"
            )
        )
    if "user_location_update_url" in cols and "participant_location_update_url" in cols:
        conn.execute(
            text(
                "UPDATE mytracks_settings SET user_location_update_url = "
                "participant_location_update_url "
                "WHERE user_location_update_url IS NULL "
                "AND participant_location_update_url IS NOT NULL"
            )
        )
    if "user_location_test_url" in cols and "participant_location_test_url" in cols:
        conn.execute(
            text(
                "UPDATE mytracks_settings SET user_location_test_url = "
                "participant_location_test_url "
                "WHERE user_location_test_url IS NULL "
                "AND participant_location_test_url IS NOT NULL"
            )
        )


def _apply_rule_user_geofence_state_last_location_migration(conn: Connection) -> None:
    inspector = inspect(conn)
    if "rule_user_geofence_state" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("rule_user_geofence_state")}
    if "last_location_received_at" in cols:
        return
    conn.execute(
        text(
            "ALTER TABLE rule_user_geofence_state "
            "ADD COLUMN last_location_received_at REAL"
        )
    )


def _apply_rule_user_location_connection_type_migration(conn: Connection) -> None:
    inspector = inspect(conn)
    for table in ("rule_user_last_location", "rule_user_location_history"):
        if table not in inspector.get_table_names():
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "connection_type" in cols:
            continue
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN connection_type VARCHAR"))


def _apply_rule_user_tables_migration(conn: Connection) -> None:
    """Copy pre-nomenclature SQLite roster/location tables when ``rule_users`` is empty."""
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "rule_users" not in tables:
        return
    user_count = conn.execute(text("SELECT COUNT(*) FROM rule_users")).scalar_one()
    if user_count != 0:
        return
    if "rule_participants" not in tables:
        return
    now = time.time()
    legacy_user_rows = conn.execute(
        text(
            "SELECT participant_id AS legacy_user_id, display_name, "
            "tracking_device_label, enabled "
            "FROM rule_participants"
        )
    ).all()
    for row in legacy_user_rows:
        first_name, last_name = parse_person_name(str(row.display_name))
        if first_name == "":
            first_name = str(row.legacy_user_id)
        conn.execute(
            text(
                "INSERT INTO rule_users "
                "(user_id, first_name, last_name, display_name, tracking_device_label, "
                "enabled, updated_at) "
                "VALUES (:user_id, :first_name, :last_name, :display_name, "
                ":tracking_device_label, :enabled, :updated_at)"
            ),
            {
                "user_id": row.legacy_user_id,
                "first_name": first_name,
                "last_name": last_name,
                "display_name": default_display_name(first_name),
                "tracking_device_label": row.tracking_device_label,
                "enabled": row.enabled,
                "updated_at": now,
            },
        )
    if "rule_participant_last_fix" in tables and "rule_user_last_location" in tables:
        location_rows = conn.execute(
            text(
                "SELECT participant_id AS legacy_user_id, lat, lon, accuracy_m, "
                "received_at, source "
                "FROM rule_participant_last_fix"
            )
        ).all()
        for row in location_rows:
            conn.execute(
                text(
                    "INSERT INTO rule_user_last_location "
                    "(user_id, lat, lon, accuracy_m, received_at, source, updated_at) "
                    "VALUES (:user_id, :lat, :lon, :accuracy_m, :received_at, :source, "
                    ":updated_at)"
                ),
                {
                    "user_id": row.legacy_user_id,
                    "lat": row.lat,
                    "lon": row.lon,
                    "accuracy_m": row.accuracy_m,
                    "received_at": row.received_at,
                    "source": row.source,
                    "updated_at": now,
                },
            )
    if (
        "rule_participant_location_history" in tables
        and "rule_user_location_history" in tables
    ):
        history_rows = conn.execute(
            text(
                "SELECT participant_id AS legacy_user_id, lat, lon, accuracy_m, "
                "received_at, source "
                "FROM rule_participant_location_history"
            )
        ).all()
        for row in history_rows:
            conn.execute(
                text(
                    "INSERT INTO rule_user_location_history "
                    "(user_id, lat, lon, accuracy_m, received_at, source, updated_at) "
                    "VALUES (:user_id, :lat, :lon, :accuracy_m, :received_at, :source, "
                    ":updated_at)"
                ),
                {
                    "user_id": row.legacy_user_id,
                    "lat": row.lat,
                    "lon": row.lon,
                    "accuracy_m": row.accuracy_m,
                    "received_at": row.received_at,
                    "source": row.source,
                    "updated_at": now,
                },
            )
