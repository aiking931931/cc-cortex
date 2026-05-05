"""concinno.tools.builtin.sql — DuckDB in-process SQL query tool.

@module sql
@responsibility A single :class:`DuckDbQuery` tool wrapping ``duckdb``
    (MIT, 37k stars) for in-process analytical SQL over the caller's
    local CSV / Parquet / JSON files. DuckDB was chosen over sqlite
    because the LLM's common ask shape — "sum this column, group by
    that column" on a file the caller just produced — maps cleanly onto
    DuckDB's ``read_csv_auto`` / ``read_parquet`` / ``read_json_auto``
    without an explicit import step.

@dependencies duckdb (optional, ``[data]`` extras). Imported lazily
    inside ``call`` so Concinno's zero-dep core is preserved.

@exports DuckDbQuery, SqlToolError

Safety contract (unsafe SQL reject)
-----------------------------------
DuckDB's SQL grammar exposes filesystem + shell reach via:

    * ``ATTACH 'remote.db'`` — opens arbitrary DB files (and URLs!).
    * ``INSTALL httpfs; LOAD httpfs;`` — turns on HTTP/S3 access.
    * ``COPY <tbl> TO '/tmp/leak.csv'`` — writes arbitrary paths.
    * ``COPY <tbl> FROM 'http://evil/x.csv'`` — reads remote.

We reject any query containing those keywords up-front with a regex.
This is not a perfect sandbox (no SQL parser in Concinno's core-dep
budget), but it blocks every documented escape path. Consumers that
need tighter isolation should run DuckDB under a Linux seccomp / chroot
downstream.

Concurrency
-----------
``is_concurrency_safe = False``. DuckDB's in-process connection object
holds state (attached databases, extensions), and we create a fresh
connection per call to sidestep any accidental sharing. The False flag
keeps the tool off the parallel dispatch path — matches CC's rule that
anything with hidden global state stays serial.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class SqlToolError(ValueError):
    """Raised for caller-visible DuckDbQuery misuse. Caught inside
    ``call`` and returned inside the error payload."""


#: Hard limit on rows returned by a single query. Prevents a ``SELECT *
#: FROM billion_row_table`` from blowing the prompt budget. Caller can
#: override via ``row_limit`` kwarg, capped at ``_MAX_ROW_LIMIT``.
DEFAULT_ROW_LIMIT = 10_000
_MAX_ROW_LIMIT = 100_000

# Keywords that grant filesystem / network reach. Checked with \bword\b
# so a column literally named ``install`` or ``copy_of_X`` does not
# accidentally trip the filter.
_UNSAFE_KEYWORDS = (
    "ATTACH",
    "DETACH",
    "INSTALL",
    "LOAD",
    "COPY",
    "EXPORT",
    "IMPORT",
    "PRAGMA",
)
_UNSAFE_PATTERN = re.compile(
    r"\b(" + "|".join(_UNSAFE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _strip_sql_comments(query: str) -> str:
    """Strip comments, string literals, and quoted identifiers so the
    keyword scanner doesn't trip on ``'INSTALL'`` string or ``"install"``
    column names — only real SQL keywords should match.

    Handles ``-- line comment``, ``/* block */``, ``'string'``
    (with ``''`` escape), and ``"quoted ident"`` (with ``""`` escape).
    """
    # Block comments first (non-greedy).
    q = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    # Line comments.
    q = re.sub(r"--[^\n]*", " ", q)
    # Single-quoted string literals (with '' escape).
    q = re.sub(r"'(?:[^']|'')*'", " ", q)
    # Double-quoted identifiers (with "" escape) — DuckDB treats these
    # as column/table names, not keywords.
    q = re.sub(r'"(?:[^"]|"")*"', " ", q)
    return q


def _reject_unsafe(query: str) -> None:
    """Raise :class:`SqlToolError` if ``query`` contains any banned
    keyword after comment stripping."""
    stripped = _strip_sql_comments(query)
    match = _UNSAFE_PATTERN.search(stripped)
    if match:
        raise SqlToolError(
            f"unsafe SQL keyword rejected: {match.group(1).upper()} "
            "(ATTACH/INSTALL/LOAD/COPY/EXPORT/IMPORT/PRAGMA/DETACH "
            "are blocked)"
        )


_READER_SUFFIX_MAP = {
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".parquet": "read_parquet",
    ".pq": "read_parquet",
    ".json": "read_json_auto",
    ".jsonl": "read_json_auto",
    ".ndjson": "read_json_auto",
}


def _resolve_file_reader(alias: str, path: str) -> tuple[str, str | None]:
    """Turn ``alias → path`` into a ``CREATE VIEW ... FROM <reader>(...)`` SQL.

    Returns ``(sql, None)`` on success or ``("", error_msg)`` on any
    validation failure. Flattens what would otherwise be a deeply
    nested loop body back under the structural-depth limit.
    """
    if not isinstance(alias, str) or not alias.isidentifier():
        return "", f"alias {alias!r} must be a valid SQL identifier"
    if not isinstance(path, str):
        return (
            "",
            f"path for {alias!r} must be a string, got {type(path).__name__}",
        )
    p = Path(path)
    if not p.exists():
        return "", f"file not found for {alias!r}: {path}"
    if not p.is_file():
        return "", f"not a regular file for {alias!r}: {path}"
    suffix = p.suffix.lower()
    reader_fn = _READER_SUFFIX_MAP.get(suffix)
    if reader_fn is None:
        return (
            "",
            (
                f"unsupported file type for {alias!r}: {suffix} "
                "(use .csv/.tsv/.parquet/.json)"
            ),
        )
    path_literal = str(p).replace("'", "''")
    # Alias validated via isidentifier() → safe in f-string.
    sql = (
        f"CREATE OR REPLACE VIEW {alias} AS "
        f"SELECT * FROM {reader_fn}('{path_literal}')"
    )
    return sql, None


class DuckDbQuery:
    """Run an analytical SQL query over caller-supplied local files.

    Attributes:
        name: ``"duckdb_query"`` — what the LLM refers to in tool calls.
        description: Short summary shown to the LLM.
        is_concurrency_safe: ``False`` — serial dispatch only.

    Example::

        tool = DuckDbQuery()
        tool.call(
            query="SELECT country, SUM(amount) FROM sales GROUP BY 1",
            files={"sales": "/tmp/sales.csv"},
        )
    """

    name: str = "duckdb_query"
    description: str = (
        "Run analytical SQL via in-process DuckDB over local CSV / "
        "Parquet / JSON files. "
        "Params: query(str) — the SELECT; "
        "files(dict[str,str]|None) — alias→path (use 'FROM alias' in "
        "SQL; alias is a view backed by read_csv_auto / read_parquet / "
        "read_json_auto based on suffix); "
        "row_limit(int=10000) — cap rows returned. "
        "Returns list[dict]. Rejects ATTACH/INSTALL/LOAD/COPY/etc."
    )
    is_concurrency_safe: bool = False

    def call(self, **kwargs: Any) -> list[dict[str, Any]] | dict[str, str]:
        query = kwargs.get("query", None)
        files = kwargs.get("files", None)
        row_limit = int(kwargs.get("row_limit", DEFAULT_ROW_LIMIT))
        row_limit = max(1, min(row_limit, _MAX_ROW_LIMIT))

        if not query or not isinstance(query, str):
            return {"error": "query is required (non-empty str)"}
        if files is not None and not isinstance(files, dict):
            return {
                "error": (
                    f"files must be dict[str,str] or None, got "
                    f"{type(files).__name__}"
                )
            }

        try:
            _reject_unsafe(query)
        except SqlToolError as exc:
            return {"error": str(exc)}

        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError as exc:
            return {
                "error": (
                    "duckdb not installed. "
                    "Run: pip install 'concinno[data]' "
                    f"(details: {exc})"
                )
            }

        conn = duckdb.connect(database=":memory:")
        try:
            for alias, path in (files or {}).items():
                view_sql, err = _resolve_file_reader(alias, path)
                if err is not None:
                    return {"error": err}
                conn.execute(view_sql)

            try:
                result = conn.execute(query).fetchmany(row_limit)
                columns = [desc[0] for desc in conn.description or []]
            except Exception as exc:  # noqa: BLE001 — duckdb raises many types
                return {"error": f"query failed: {exc}"}

            return [dict(zip(columns, row, strict=False)) for row in result]
        finally:
            conn.close()


__all__ = ["DuckDbQuery", "SqlToolError"]
