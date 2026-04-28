"""Tests for concinno.security.sql_injection_guard (4.6.0 W4 wave-1).

Covers the 5 unsafe SQL construction styles, the 4 safe whitelist
shapes, file-extension gating, docstring / comment skip, test-fixture
skip, payload extraction across Edit / Write / NotebookEdit /
MultiEdit / Bash, the BaseGuard pipeline adapter wiring, and an
end-to-end PreToolUse hook test that proves the guard fires through
``create_default_pipeline``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from concinno.security import (
    PolicyGateResult,
    SqlInjectionFinding,
    SqlInjectionGuard,
    extract_sql_payload,
)
from concinno.security import sql_injection_guard as sql_mod
from concinno.security.sql_injection_guard import (
    SqlInjectionBaseGuard,
    _SqlPayload,
)

# ── Shared fixtures ─────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Redirect audit writes to a per-test tmp dir + disable ZIQ bus."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


@pytest.fixture
def guard_low(audit_tmp: Path) -> SqlInjectionGuard:
    """Most permissive guard — captures every shape incl. low severity."""
    return SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        skip_test_files=False,
    )


@pytest.fixture
def guard_default(audit_tmp: Path) -> SqlInjectionGuard:
    """Default guard — min_severity='medium', skip_test_files=True."""
    return SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
    )


def _types(result: PolicyGateResult) -> set[str]:
    return {f.type for f in result.findings}


def _payload(code: str, *, path: str = "src/app/db.py") -> _SqlPayload:
    return _SqlPayload(
        code=code,
        file_path=path,
        tool_name="Edit",
        language_hint="py",
    )


# ════════════════════════════════════════════════════════════════
# 1. Concatenation (severity high / critical)
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = "SELECT * FROM users WHERE id=" + uid',
        'q = "SELECT * FROM users WHERE name=" + str(name)',
        'q = "DELETE FROM logs WHERE id=" + log_id + " AND ts>0"',
        'cur.execute("UPDATE items SET qty=" + qty)',
        'sql = "INSERT INTO t VALUES (" + value + ")"',
    ],
)
def test_concat_positive(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert "sqli.concat" in _types(result)


def test_concat_user_input_marker_promotes_to_critical(
    guard_low: SqlInjectionGuard,
) -> None:
    code = 'q = "SELECT * FROM users WHERE id=" + user_input'
    result = guard_low.evaluate(_payload(code))
    crit = [
        f for f in result.findings
        if f.type == "sqli.concat" and f.severity == "critical"
    ]
    assert crit, "user_input marker should promote concat to critical"


def test_concat_request_marker_promotes_to_critical(
    guard_low: SqlInjectionGuard,
) -> None:
    code = 'q = "SELECT * FROM users WHERE id=" + request.args["id"]'
    result = guard_low.evaluate(_payload(code))
    crit = [
        f for f in result.findings
        if f.type == "sqli.concat" and f.severity == "critical"
    ]
    assert crit


# ════════════════════════════════════════════════════════════════
# 2. f-string (severity high)
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = f"SELECT * FROM users WHERE id={uid}"',
        'q = f"SELECT * FROM users WHERE name={name!r}"',
        'cur.execute(f"UPDATE items SET qty={qty} WHERE id={iid}")',
        "q = rf'SELECT * FROM logs WHERE level={lvl}'",
        'q = f"DELETE FROM t WHERE col={col}"',
    ],
)
def test_fstring_positive(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert "sqli.fstring" in _types(result)


def test_fstring_severity_is_high(
    guard_low: SqlInjectionGuard,
) -> None:
    code = 'q = f"SELECT * FROM users WHERE id={uid}"'
    result = guard_low.evaluate(_payload(code))
    fstring = [f for f in result.findings if f.type == "sqli.fstring"]
    assert fstring and fstring[0].severity == "high"


# ════════════════════════════════════════════════════════════════
# 3. % formatting (severity medium)
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = "SELECT * FROM users WHERE id=%s" % uid',
        'q = "SELECT * FROM users WHERE name=%(name)s" % d',
        'sql = "DELETE FROM t WHERE col=%s AND k=%s" % (a, b)',
    ],
)
def test_percent_positive(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert "sqli.percent" in _types(result)


# ════════════════════════════════════════════════════════════════
# 4. .format() (severity medium)
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = "SELECT * FROM users WHERE id={}".format(uid)',
        'q = "SELECT * FROM logs WHERE level={}".format(lvl)',
        'cur.execute("UPDATE items SET qty={}".format(q))',
    ],
)
def test_format_positive(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert "sqli.format" in _types(result)


# ════════════════════════════════════════════════════════════════
# 5. Dynamic identifier (severity low)
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = f"SELECT * FROM {table}"',
        'q = f"UPDATE {schema}.{tbl} SET x=1"',
        'q = f"INSERT INTO {tbl} VALUES (1)"',
        'q = f"SELECT a FROM t JOIN {other} ON t.id = {other}.id"',
    ],
)
def test_dynamic_identifier_positive(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    types = _types(result)
    # dynamic identifier always also fires fstring, so accept either
    # discriminator
    assert {"sqli.dynamic_identifier", "sqli.fstring"} & types


# ════════════════════════════════════════════════════════════════
# 6. Whitelist — parametrized DB-API
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'cur.execute("SELECT * FROM users WHERE id=?", (uid,))',
        'cur.execute("SELECT * FROM users WHERE id=:id", {"id": uid})',
        'cur.execute("SELECT ... WHERE id=%(id)s", {"id": uid})',
    ],
)
def test_whitelist_parametrized(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert not result.findings, (
        f"parametrized query should be clean, got {_types(result)}"
    )


# ════════════════════════════════════════════════════════════════
# 7. Whitelist — SQLAlchemy text() bindparams
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = text("SELECT * FROM users WHERE id=:id").bindparams(id=uid)',
        'stmt = text("UPDATE t SET x=:x").bindparam(x=val)',
    ],
)
def test_whitelist_sqlalchemy_text(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert not result.findings


# ════════════════════════════════════════════════════════════════
# 8. Whitelist — ORM filter syntax (Django + SQLAlchemy ORM)
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        "rows = User.objects.filter(id=uid)",
        "rows = session.query(User).filter_by(id=uid).first()",
        "rows = User.query.filter(User.id == uid).all()",
        "row = User.objects.get(id=uid)",
    ],
)
def test_whitelist_orm(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    # ORM call alone has no SQL keyword → no scan, but also include
    # SQL-keyword context to assert the whitelist short-circuits.
    sql_ctx = "# SELECT FROM users\n" + code
    result = guard_low.evaluate(_payload(sql_ctx))
    assert not result.findings


# ════════════════════════════════════════════════════════════════
# 9. Whitelist — psycopg.sql.Identifier composition
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        'q = psycopg.sql.SQL("SELECT * FROM {}").format(psycopg.sql.Identifier(table))',
        'q = psycopg2.sql.SQL("UPDATE {} SET x=%s").format(psycopg2.sql.Identifier(t))',
    ],
)
def test_whitelist_psycopg_sql(
    guard_low: SqlInjectionGuard, code: str
) -> None:
    result = guard_low.evaluate(_payload(code))
    assert not result.findings


# ════════════════════════════════════════════════════════════════
# 10. Edge cases — docstrings, comments, test fixtures
# ════════════════════════════════════════════════════════════════


def test_docstring_skip(guard_low: SqlInjectionGuard) -> None:
    code = '''
def foo():
    """Example: SELECT * FROM users WHERE id={uid}.

    The f-string SELECT pattern in this docstring is documentation,
    not code. q = f"SELECT * FROM users WHERE id={uid}" — these lines
    must not flag.
    """
    return 0
'''
    result = guard_low.evaluate(_payload(code))
    assert not result.findings


def test_comment_skip(guard_low: SqlInjectionGuard) -> None:
    code = '# q = f"SELECT * FROM users WHERE id={uid}"  — example only'
    result = guard_low.evaluate(_payload(code))
    assert not result.findings


def test_inline_comment_split(guard_low: SqlInjectionGuard) -> None:
    code = (
        'rows = User.objects.filter(id=uid)  '
        '# SELECT user by id — no actual SQL string here'
    )
    result = guard_low.evaluate(_payload(code))
    assert not result.findings


def test_test_fixture_skip(audit_tmp: Path) -> None:
    """When skip_test_files=True (default) test paths skip negative
    fixtures like ``' OR 1=1 --``."""
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        skip_test_files=True,
    )
    code = (
        'def test_sqli_fixture():\n'
        '    """Negative test data."""\n'
        '    payload = "\' OR 1=1 -- SELECT * FROM users"\n'
        '    assert payload  # SELECT inside test bait\n'
    )
    result = guard.evaluate(_payload(code, path="tests/security/test_x.py"))
    # Test path + assert / test_ markers → skip
    assert not result.findings


def test_test_fixture_no_skip_when_disabled(audit_tmp: Path) -> None:
    """skip_test_files=False keeps every match including in tests."""
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        skip_test_files=False,
    )
    code = 'q = f"SELECT * FROM users WHERE id={uid}"'
    result = guard.evaluate(_payload(code, path="tests/security/test_x.py"))
    assert "sqli.fstring" in _types(result)


# ════════════════════════════════════════════════════════════════
# 11. min_severity filter
# ════════════════════════════════════════════════════════════════


def test_min_severity_medium_drops_dynamic_identifier(
    audit_tmp: Path,
) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="medium",
    )
    # Dynamic-identifier-only payload would only emit `sqli.dynamic_identifier`
    # at low severity; we use a payload that has both fstring (high) and
    # the dynamic-identifier shape, then check we see fstring (kept) but
    # no low-severity findings.
    code = 'q = f"SELECT * FROM {table}"'
    result = guard.evaluate(_payload(code))
    types = _types(result)
    # fstring is high → kept; dynamic_identifier is low → dropped under medium
    assert "sqli.fstring" in types
    assert "sqli.dynamic_identifier" not in types


def test_min_severity_high_drops_percent_and_format(
    audit_tmp: Path,
) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="high",
    )
    code_pct = 'q = "SELECT * FROM users WHERE id=%s" % uid'
    code_fmt = 'q = "SELECT * FROM users WHERE id={}".format(uid)'
    assert not guard.evaluate(_payload(code_pct)).findings
    assert not guard.evaluate(_payload(code_fmt)).findings


def test_min_severity_critical_keeps_only_user_input_concat(
    audit_tmp: Path,
) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="critical",
    )
    crit_code = 'q = "SELECT * FROM users WHERE id=" + user_input'
    plain_concat = 'q = "SELECT * FROM users WHERE id=" + uid'
    assert "sqli.concat" in _types(guard.evaluate(_payload(crit_code)))
    assert not guard.evaluate(_payload(plain_concat)).findings


# ════════════════════════════════════════════════════════════════
# 12. Payload extraction
# ════════════════════════════════════════════════════════════════


def test_extract_edit_python() -> None:
    payload = extract_sql_payload(
        "Edit",
        {"file_path": "/tmp/x.py", "new_string": 'q = f"SELECT FROM {t}"'},
    )
    assert payload is not None
    assert payload.tool_name == "Edit"
    assert payload.language_hint == "py"


def test_extract_write_sql_file() -> None:
    payload = extract_sql_payload(
        "Write",
        {"file_path": "/tmp/x.sql", "content": "SELECT 1"},
    )
    assert payload is not None
    assert payload.language_hint == "sql"


def test_extract_skips_markdown() -> None:
    payload = extract_sql_payload(
        "Write",
        {"file_path": "/tmp/x.md", "content": 'q = f"SELECT FROM {t}"'},
    )
    assert payload is None


def test_extract_skips_txt() -> None:
    payload = extract_sql_payload(
        "Edit",
        {"file_path": "/tmp/x.txt", "new_string": "anything"},
    )
    assert payload is None


def test_extract_skips_unknown_extension() -> None:
    payload = extract_sql_payload(
        "Edit",
        {"file_path": "/tmp/x.foo", "new_string": "anything"},
    )
    assert payload is None


def test_extract_notebook_edit() -> None:
    payload = extract_sql_payload(
        "NotebookEdit",
        {
            "notebook_path": "/tmp/x.ipynb",
            "new_source": 'q = f"SELECT FROM {t}"',
        },
    )
    assert payload is not None
    assert payload.tool_name == "NotebookEdit"


def test_extract_multiedit() -> None:
    payload = extract_sql_payload(
        "MultiEdit",
        {
            "file_path": "/tmp/x.py",
            "edits": [
                {"old_string": "a", "new_string": 'q1 = f"SELECT FROM {t}"'},
                {"old_string": "b", "new_string": "q2 = 0"},
            ],
        },
    )
    assert payload is not None
    assert "SELECT FROM" in payload.code


def test_extract_bash_with_python() -> None:
    payload = extract_sql_payload(
        "Bash",
        {"command": "python -c 'print(1)'"},
    )
    assert payload is not None
    assert payload.tool_name == "Bash"


def test_extract_bash_skips_non_python_command() -> None:
    payload = extract_sql_payload(
        "Bash", {"command": "ls -la"}
    )
    assert payload is None


def test_extract_skips_unknown_tool() -> None:
    payload = extract_sql_payload(
        "Read", {"file_path": "/tmp/x.py"}
    )
    assert payload is None


def test_extract_handles_missing_keys() -> None:
    assert extract_sql_payload("Edit", {}) is None
    assert extract_sql_payload("Edit", {"file_path": ""}) is None


# ════════════════════════════════════════════════════════════════
# 13. Escape hatch
# ════════════════════════════════════════════════════════════════


def test_escape_hatch(guard_low: SqlInjectionGuard) -> None:
    code = (
        'q = f"SELECT * FROM users WHERE id={uid}"  '
        "# CONCINNO_DISABLE: trusted query template"
    )
    result = guard_low.evaluate(_payload(code))
    assert result.escaped is True
    assert result.decision == "accept"


# ════════════════════════════════════════════════════════════════
# 14. Audit + ZIQ integration (via PolicyGate base)
# ════════════════════════════════════════════════════════════════


def test_audit_log_written_on_warn_log(
    audit_tmp: Path,
) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn+log",
        min_severity="low",
        skip_test_files=False,
    )
    code = 'q = f"SELECT * FROM users WHERE id={uid}"'
    guard.evaluate(_payload(code))
    log_path = audit_tmp / "sql_injection_guard.jsonl"
    assert log_path.exists()
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    entry = json.loads(line)
    assert entry["guard"] == "sql_injection_guard"
    assert entry["fail_mode"] == "warn+log"
    assert entry["findings"]


def test_no_audit_log_on_silent(audit_tmp: Path) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="silent",
        min_severity="low",
        skip_test_files=False,
    )
    guard.evaluate(_payload('q = f"SELECT FROM {t}"'))
    assert not (audit_tmp / "sql_injection_guard.jsonl").exists()


# ════════════════════════════════════════════════════════════════
# 15. Coercion of raw payloads
# ════════════════════════════════════════════════════════════════


def test_str_payload_accepted(audit_tmp: Path) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
    )
    result = guard.evaluate('q = f"SELECT FROM {t}"')
    assert "sqli.fstring" in _types(result)


def test_bytes_payload_accepted(audit_tmp: Path) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
    )
    result = guard.evaluate(b'q = f"SELECT FROM {t}"')
    assert "sqli.fstring" in _types(result)


def test_dict_payload_with_tool_input(audit_tmp: Path) -> None:
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
    )
    payload: dict[str, Any] = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "/tmp/x.py",
            "new_string": 'q = f"SELECT FROM {t}"',
        },
    }
    result = guard.evaluate(payload)
    assert "sqli.fstring" in _types(result)


def test_dict_payload_unknown_tool(audit_tmp: Path) -> None:
    """dict with tool_name='Read' (out of scope) accepts cleanly."""
    guard = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
    )
    payload: dict[str, Any] = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x.py"},
    }
    result = guard.evaluate(payload)
    assert result.decision == "accept"
    assert not result.findings


# ════════════════════════════════════════════════════════════════
# 16. Severity / Finding shape
# ════════════════════════════════════════════════════════════════


def test_finding_redacted_snippet_capped(
    guard_low: SqlInjectionGuard,
) -> None:
    long_line = (
        'q = f"SELECT * FROM users WHERE id={uid}'
        + " AND name='" + "x" * 200 + "'"
        + '"'
    )
    result = guard_low.evaluate(_payload(long_line))
    fstring = [f for f in result.findings if f.type == "sqli.fstring"]
    assert fstring
    # The PolicyGate base class additionally caps to 80 chars during
    # serialisation; our internal cap is 80 too.
    assert len(fstring[0].snippet) <= 80


def test_finding_is_re_exported_as_alias() -> None:
    from concinno.security.policy_gate import Finding

    assert SqlInjectionFinding is Finding


# ════════════════════════════════════════════════════════════════
# 17. BaseGuard pipeline adapter — wiring acceptance test
# ════════════════════════════════════════════════════════════════


def test_pipeline_adapter_class_exported() -> None:
    """``SqlInjectionBaseGuard`` is importable and is a BaseGuard
    subclass on production installs."""
    from concinno.guards.base import BaseGuard

    assert SqlInjectionBaseGuard is not None
    assert issubclass(SqlInjectionBaseGuard, BaseGuard)
    assert SqlInjectionBaseGuard.name == "sql_injection_guard"
    assert SqlInjectionBaseGuard.feature_name == "sql_injection_guard"


def test_pipeline_adapter_registered_in_default_pipeline() -> None:
    """The default pipeline contains a ``sql_injection_guard`` entry."""
    from concinno.guards.registry import create_default_pipeline

    pipe = create_default_pipeline()
    names = [g.name for g in pipe._guards]
    assert "sql_injection_guard" in names


def test_pipeline_adapter_check_returns_none_on_clean_payload(
    audit_tmp: Path,
) -> None:
    """Clean Edit input → adapter returns None (no opinion)."""
    from concinno.guards.base import GuardContext

    ag = SqlInjectionBaseGuard()  # type: ignore[misc]
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={
            "file_path": "/tmp/x.py",
            "new_string": "rows = User.objects.filter(id=uid)",
        },
        session_id="s1",
        cache_dir="",
        hook_event="PreToolUse",
    )
    assert ag.check(ctx) is None


def test_pipeline_adapter_check_warns_on_dirty_payload(
    audit_tmp: Path,
) -> None:
    """Dirty f-string Edit input → adapter advises (allow_advisory)."""
    from concinno.guards.base import GuardAction, GuardContext

    ag = SqlInjectionBaseGuard()  # type: ignore[misc]
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={
            "file_path": "/tmp/x.py",
            "new_string": 'q = f"SELECT * FROM users WHERE id={uid}"',
        },
        session_id="s1",
        cache_dir="",
        hook_event="PreToolUse",
    )
    res = ag.check(ctx)
    assert res is not None
    assert res.action == GuardAction.ALLOW
    assert "SqlInjectionGuard" in res.context


def test_pipeline_adapter_check_denies_when_pinned_to_hard_deny(
    audit_tmp: Path,
) -> None:
    """Inject a hard_deny-pinned guard → adapter returns DENY."""
    from concinno.guards.base import GuardAction, GuardContext

    inner = SqlInjectionGuard(
        profile="lite",
        fail_mode_override="hard_deny",
        min_severity="low",
        skip_test_files=False,
    )
    ag = SqlInjectionBaseGuard(guard=inner)  # type: ignore[misc]
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={
            "file_path": "/tmp/x.py",
            "new_string": 'q = f"SELECT * FROM users WHERE id={uid}"',
        },
        session_id="s1",
        cache_dir="",
        hook_event="PreToolUse",
    )
    res = ag.check(ctx)
    assert res is not None
    assert res.action == GuardAction.DENY
    assert "sqli." in res.reason or "SQL injection" in res.reason


def test_pipeline_adapter_skips_out_of_scope_extension(
    audit_tmp: Path,
) -> None:
    """``.md`` Edit → adapter returns None (out of scope)."""
    from concinno.guards.base import GuardContext

    ag = SqlInjectionBaseGuard()  # type: ignore[misc]
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={
            "file_path": "/tmp/x.md",
            "new_string": 'q = f"SELECT FROM {t}"',
        },
        session_id="s1",
        cache_dir="",
        hook_event="PreToolUse",
    )
    assert ag.check(ctx) is None


# ════════════════════════════════════════════════════════════════
# 18. Module-level imports
# ════════════════════════════════════════════════════════════════


def test_module_exports_match_dunder_all() -> None:
    """Public exports match ``__all__``."""
    expected = {
        "SqlInjectionFinding",
        "SqlInjectionGuard",
        "SqlInjectionBaseGuard",
        "extract_sql_payload",
    }
    assert expected.issubset(set(sql_mod.__all__))
