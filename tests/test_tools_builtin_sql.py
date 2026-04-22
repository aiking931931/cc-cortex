"""Tests for concinno.tools.builtin.sql — DuckDbQuery."""

from __future__ import annotations

import json

import pytest

from concinno.tools.builtin.sql import (
    DuckDbQuery,
    SqlToolError,
    _reject_unsafe,
    _strip_sql_comments,
)


# --------------------------------------------------------------------------- #
# Safety filter unit tests — run without duckdb installed.                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "query",
    [
        "ATTACH 'foo.db'",
        "attach 'foo.db'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "COPY tbl TO '/tmp/x.csv'",
        "EXPORT DATABASE 'dump'",
        "IMPORT DATABASE 'dump'",
        "PRAGMA show_tables",
        "DETACH db",
        "SELECT 1; ATTACH 'x.db'",
    ],
)
def test_reject_unsafe_positive(query):
    with pytest.raises(SqlToolError, match="unsafe SQL keyword"):
        _reject_unsafe(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT name FROM sales GROUP BY 1",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        # Column literally named 'install' — should NOT trip the filter.
        'SELECT "install" FROM t',
        # SQL comments are not executed — keyword inside comment is safe.
        "SELECT 1 /* ATTACH 'x.db' */",
        "SELECT 1 -- INSTALL httpfs",
        # String literal containing keyword — not a SQL keyword.
        "SELECT 'INSTALL httpfs' AS note",
    ],
)
def test_reject_unsafe_negative(query):
    _reject_unsafe(query)  # no raise


def test_strip_sql_comments_line():
    assert "ATTACH" not in _strip_sql_comments("SELECT 1 -- ATTACH 'x.db'")


def test_strip_sql_comments_block():
    assert "INSTALL" not in _strip_sql_comments("SELECT 1 /* INSTALL httpfs */")


# --------------------------------------------------------------------------- #
# Fixture: CSV file on disk.                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sales_csv(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(
        "country,amount\n"
        "US,100\n"
        "US,250\n"
        "UK,80\n"
        "UK,20\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def nums_json(tmp_path):
    path = tmp_path / "nums.json"
    path.write_text(
        json.dumps([{"n": 1}, {"n": 2}, {"n": 3}]),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# DuckDbQuery — end-to-end                                                    #
# --------------------------------------------------------------------------- #


def test_duckdb_attributes():
    assert DuckDbQuery.name == "duckdb_query"
    assert DuckDbQuery.is_concurrency_safe is False
    assert "duckdb" in DuckDbQuery.description.lower()


def test_duckdb_missing_query():
    tool = DuckDbQuery()
    assert tool.call() == {"error": "query is required (non-empty str)"}


def test_duckdb_wrong_files_type():
    tool = DuckDbQuery()
    out = tool.call(query="SELECT 1", files=["not a dict"])
    assert "error" in out
    assert "files must be dict" in out["error"]


def test_duckdb_unsafe_rejected():
    tool = DuckDbQuery()
    out = tool.call(query="ATTACH 'evil.db'")
    assert "error" in out
    assert "unsafe SQL keyword" in out["error"]


def test_duckdb_simple_select():
    pytest.importorskip("duckdb")
    tool = DuckDbQuery()
    out = tool.call(query="SELECT 1 AS x, 'a' AS y")
    assert out == [{"x": 1, "y": "a"}]


def test_duckdb_csv_alias(sales_csv):
    pytest.importorskip("duckdb")
    tool = DuckDbQuery()
    out = tool.call(
        query="SELECT country, SUM(amount) AS total "
        "FROM sales GROUP BY country ORDER BY country",
        files={"sales": str(sales_csv)},
    )
    assert isinstance(out, list)
    assert {"country": "UK", "total": 100} in out
    assert {"country": "US", "total": 350} in out


def test_duckdb_json_alias(nums_json):
    pytest.importorskip("duckdb")
    tool = DuckDbQuery()
    out = tool.call(
        query="SELECT SUM(n) AS total FROM nums",
        files={"nums": str(nums_json)},
    )
    assert out == [{"total": 6}]


def test_duckdb_alias_not_identifier():
    tool = DuckDbQuery()
    out = tool.call(
        query="SELECT 1",
        files={"bad-alias": "anything.csv"},
    )
    assert "error" in out
    assert "valid SQL identifier" in out["error"]


def test_duckdb_alias_file_missing(tmp_path):
    tool = DuckDbQuery()
    out = tool.call(
        query="SELECT 1",
        files={"t": str(tmp_path / "nope.csv")},
    )
    assert "error" in out
    assert "file not found" in out["error"]


def test_duckdb_alias_unsupported_suffix(tmp_path):
    tool = DuckDbQuery()
    bad = tmp_path / "x.xlsx"
    bad.write_bytes(b"fake")
    out = tool.call(query="SELECT 1", files={"t": str(bad)})
    assert "error" in out
    assert "unsupported file type" in out["error"]


def test_duckdb_row_limit(sales_csv):
    pytest.importorskip("duckdb")
    tool = DuckDbQuery()
    out = tool.call(
        query="SELECT * FROM sales",
        files={"sales": str(sales_csv)},
        row_limit=2,
    )
    assert isinstance(out, list)
    assert len(out) == 2
