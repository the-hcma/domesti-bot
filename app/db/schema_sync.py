"""Align on-disk SQLite tables with ORM column definitions."""

from __future__ import annotations

from sqlalchemy import Column, inspect, text
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateColumn

from app.db.base import Base


def sync_missing_columns(engine: Engine) -> None:
    """Add ORM columns that are absent from existing tables (SQLite ``ALTER TABLE``)."""
    with engine.begin() as conn:
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())
        for table_name, table in sorted(Base.metadata.tables.items(), key=lambda item: item[0]):
            if table_name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                ddl = _sqlite_add_column_ddl(column, dialect=engine.dialect)
                try:
                    conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {ddl}'))
                except OperationalError as exc:
                    message = str(exc).lower()
                    if "duplicate column name" not in message:
                        raise
                existing_cols.add(column.name)


def _sqlite_add_column_ddl(column: Column[object], *, dialect: Dialect) -> str:
    ddl = str(CreateColumn(column).compile(dialect=dialect))
    if " NOT NULL" not in ddl.upper():
        return ddl
    if "DEFAULT" in ddl.upper():
        return ddl
    default_sql = _sqlite_default_sql(column)
    if default_sql is not None:
        return f"{ddl} DEFAULT {default_sql}"
    return ddl.replace(" NOT NULL", "").replace(" not null", "")


def _sqlite_default_sql(column: Column[object]) -> str | None:
    default = column.default
    if default is None:
        return None
    value = getattr(default, "arg", None)
    if value is None or callable(value):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None
