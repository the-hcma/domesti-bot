"""SQLAlchemy persistence for discovery cache and encrypted application secrets."""

from __future__ import annotations

from app.db.engine import dispose_engine, get_engine
from app.db.legacy_migrations import apply_legacy_column_migrations
from app.db.schema import bootstrap_schema, ensure_schema_if_exists
from app.db.secrets import (
    delete_app_secret,
    load_tailwind_token_from_db,
    save_tailwind_token_to_db,
    secrets_key_configured,
    secrets_key_source,
)
from app.db.secrets_key import (
    generate_fernet_key,
    load_secrets_key_material,
    secrets_json_path,
    write_secrets_json,
)

__all__ = [
    "apply_legacy_column_migrations",
    "delete_app_secret",
    "dispose_engine",
    "bootstrap_schema",
    "ensure_schema_if_exists",
    "get_engine",
    "load_tailwind_token_from_db",
    "save_tailwind_token_to_db",
    "generate_fernet_key",
    "load_secrets_key_material",
    "secrets_json_path",
    "write_secrets_json",
    "secrets_key_configured",
    "secrets_key_source",
]
