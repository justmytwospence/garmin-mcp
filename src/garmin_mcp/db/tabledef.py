"""Declarative typed tables: every column has a type, a comment with its unit, and the
SQL expression that reads it from the JSON object aliased ``b`` (plus whatever context
aliases the projection provides: ``src``, ``s`` ...). DDL, projections and the drift
check are generated from these definitions, so a column cannot be declared without a
source or mapped without a declaration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    comment: str
    expr: str
    # Top-level JSON key this column consumes (for the drift check); None for context columns.
    key: str | None


@dataclass
class Table:
    name: str
    comment: str
    primary_key: list[str]
    columns: list[Column] = field(default_factory=list)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)


def default_expr(key: str, type_: str, obj: str = "b") -> str:
    if type_ == "JSON":
        return f"{obj}->'{key}'"
    if type_.endswith("[]"):
        return f"TRY_CAST({obj}->'{key}' AS {type_})"
    if type_ == "TIMESTAMP":
        return f"TRY_CAST(left({obj}->>'{key}', 19) AS TIMESTAMP)"
    if type_ == "VARCHAR":
        return f"{obj}->>'{key}'"
    if type_ == "DATE":
        return f"TRY_CAST(left({obj}->>'{key}', 10) AS DATE)"
    return f"TRY_CAST({obj}->>'{key}' AS {type_})"


def col(name: str, type_: str, comment: str, key: str | None = None) -> Column:
    """A column read straight from the JSON key of the same name (or `key`)."""
    k = key or name
    return Column(name, type_, comment, default_expr(k, type_), k)


def expr(name: str, type_: str, comment: str, sql: str, key: str | None = None) -> Column:
    """A column computed by an explicit expression; `key` names the JSON key it consumes."""
    return Column(name, type_, comment, sql, key)


def epoch_ms(name: str, type_: str, comment: str, key: str | None = None) -> Column:
    """A TIMESTAMPTZ column from a Garmin epoch-milliseconds field (GMT)."""
    k = key or name
    return Column(name, type_, comment, f"to_timestamp(TRY_CAST({'b'}->>'{k}' AS BIGINT) / 1000.0)", k)


def local_ms(name: str, comment: str, key: str | None = None) -> Column:
    """A TIMESTAMP (wall clock) column from a Garmin local epoch-milliseconds field."""
    k = key or name
    return Column(name, "TIMESTAMP", comment, f"epoch_ms(TRY_CAST(b->>'{k}' AS BIGINT))::TIMESTAMP", k)


def sql_string(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def table_ddl(t: Table) -> list[str]:
    cols = ",\n".join(f'  "{c.name}" {c.type}' for c in t.columns)
    pk = ", ".join(f'"{k}"' for k in t.primary_key)
    return [
        f"CREATE TABLE IF NOT EXISTS {t.name} (\n{cols},\n  PRIMARY KEY ({pk})\n)",
        f"COMMENT ON TABLE {t.name} IS {sql_string(t.comment)}",
        *[f'COMMENT ON COLUMN {t.name}."{c.name}" IS {sql_string(c.comment)}' for c in t.columns],
    ]


def select_list(t: Table, only: Callable[[Column], bool] | None = None) -> tuple[str, str]:
    cols = [c for c in t.columns if only is None or only(c)]
    names = ", ".join(f'"{c.name}"' for c in cols)
    exprs = ",\n    ".join(f'{c.expr} AS "{c.name}"' for c in cols)
    return names, exprs


def consumed_keys(t: Table) -> set[str]:
    return {c.key for c in t.columns if c.key is not None}


def insert_sql(t: Table, source: str, only: Callable[[Column], bool] | None = None, conflict: str = "OR REPLACE") -> str:
    names, exprs = select_list(t, only)
    return f"INSERT {conflict} INTO {t.name} ({names})\n  SELECT\n    {exprs}\n  FROM {source}"


def upsert_sql(t: Table, source: str, only: Callable[[Column], bool]) -> str:
    """Upsert that only overwrites the given columns of an existing row."""
    names, exprs = select_list(t, only)
    updates = ", ".join(
        f'"{c.name}" = excluded."{c.name}"' for c in t.columns if only(c) and c.name not in t.primary_key
    )
    pk = ", ".join(f'"{k}"' for k in t.primary_key)
    return f"INSERT INTO {t.name} ({names})\n  SELECT\n    {exprs}\n  FROM {source}\n  ON CONFLICT ({pk}) DO UPDATE SET {updates}"
