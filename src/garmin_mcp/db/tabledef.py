"""Declarative typed tables: every column has a type, a comment with its unit, and the
SQL expression that reads it from the JSON object aliased ``b`` (plus whatever context
aliases the projection provides: ``src``, ``s`` ...). DDL, projections and the drift
check are generated from these definitions, so a column cannot be declared without a
source or mapped without a declaration.
"""

from __future__ import annotations

import re
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


def fill_extra(t: Table, also_consumed: dict[str, str] | None = None) -> None:
    """A column named extra with no key becomes the JSON object minus every key the table
    consumes. `also_consumed` names, per consumed key, a second key that the same column
    consumes (a composite column reading the enhanced and the plain FIT field)."""
    for i, c in enumerate(t.columns):
        if c.expr == "__EXTRA__":
            consumed = {x.key for x in t.columns if x.key is not None}
            consumed |= {(also_consumed or {})[k] for k in list(consumed) if k in (also_consumed or {})}
            patch = "{" + ",".join(f'"{k}":null' for k in sorted(consumed)) + "}"
            t.columns[i] = Column(c.name, c.type, c.comment, f"json_merge_patch(b, '{patch}')", None)


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


_EXTRACT = re.compile(r"\b([bd])(->>|->)'([^']+)'")
_ALIAS = re.compile(r"\)\s+(\w+)(\s+WHERE\s.*)?\s*$", re.DOTALL)
# Above this many JSON extractions per row, parse each body once into lists instead.
_WIDE = 24


def _parse_once(exprs: str, source: str) -> tuple[str, str]:
    """Rewrite top-level `b->>'k'` / `b->'k'` (and `d`) extractions into indexes of lists
    built by one json_extract_string / json_extract call per body.

    DuckDB parses the JSON afresh for every extraction expression and keeps a parser
    arena per expression, so a 200-column projection of wide Garmin payloads needs
    hundreds of MB; one multi-path extraction per body keeps that to a few.
    """
    found = _EXTRACT.findall(exprs)
    m = re.search(r"\)\s+(\w+)\s*$", source) or _ALIAS.search(source)
    if len(found) < _WIDE or not m or not _single_subquery(source[: m.start() + 1]):
        return exprs, source
    paths: dict[tuple[str, str], list[str]] = {}
    for obj, op, key in found:
        lst = paths.setdefault((obj, op), [])
        if key not in lst:
            lst.append(key)

    def repl(match: re.Match[str]) -> str:
        obj, op, key = match.groups()
        idx = paths[(obj, op)].index(key) + 1
        return f"__{obj}{'s' if op == '->>' else 'j'}[{idx}]"

    new_exprs = _EXTRACT.sub(repl, exprs)
    lists = ", ".join(
        f"{'json_extract_string' if op == '->>' else 'json_extract'}({obj}, [{', '.join(sql_string('$.' + json_path_key(k)) for k in keys)}]) AS __{obj}{'s' if op == '->>' else 'j'}"
        for (obj, op), keys in paths.items()
    )
    alias = m.group(1)
    return new_exprs, f"(SELECT *, {lists} FROM {source}) {alias}"


def _single_subquery(text: str) -> bool:
    """Whether text is exactly one parenthesized subquery (so the alias after it names all of the source)."""
    text = text.strip()
    if not text.startswith("("):
        return False
    depth = 0
    in_str = False
    for i, ch in enumerate(text):
        if in_str:
            in_str = ch != "'"
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


def json_path_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else '"' + key.replace('"', '\\"') + '"'


def insert_sql(t: Table, source: str, only: Callable[[Column], bool] | None = None, conflict: str = "OR REPLACE") -> str:
    names, exprs = select_list(t, only)
    exprs, source = _parse_once(exprs, source)
    return f"INSERT {conflict} INTO {t.name} ({names})\n  SELECT\n    {exprs}\n  FROM {source}"


def upsert_sql(t: Table, source: str, only: Callable[[Column], bool]) -> str:
    """Upsert that only overwrites the given columns of an existing row."""
    names, exprs = select_list(t, only)
    exprs, source = _parse_once(exprs, source)
    updates = ", ".join(
        f'"{c.name}" = excluded."{c.name}"' for c in t.columns if only(c) and c.name not in t.primary_key
    )
    pk = ", ".join(f'"{k}"' for k in t.primary_key)
    return f"INSERT INTO {t.name} ({names})\n  SELECT\n    {exprs}\n  FROM {source}\n  ON CONFLICT ({pk}) DO UPDATE SET {updates}"
