"""Tests for concinno.secret_scan — Hardcoded secret detection."""

from __future__ import annotations

from concinno.secret_scan import check


class TestSecretScanCheck:
    # ── Should block ──────────────────────────────────

    def test_blocks_aws_access_key(self):
        r = check("Write", {"file_path": "config.py", "content": 'key = "AKIAIOSFODNN7EXAMPLE"'})
        assert r is not None
        assert r["permissionDecision"] == "deny"
        assert "AWS Access Key" in r["secrets"]

    def test_blocks_github_token(self):
        gh_token = "ghp_" + "A" * 36
        r = check("Write", {"file_path": "x.py", "content": f'token = "{gh_token}"'})
        assert r is not None
        assert "GitHub Token" in r["secrets"]

    def test_blocks_private_key(self):
        r = check("Edit", {
            "file_path": "deploy.sh",
            "new_string": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
        })
        assert r is not None
        assert "Private Key" in r["secrets"]

    def test_blocks_slack_token(self):
        r = check("Write", {"file_path": "bot.py", "content": 'SLACK = "xoxb-1234-abcdefghij"'})
        assert r is not None
        assert "Slack Token" in r["secrets"]

    def test_blocks_api_key_assignment(self):
        r = check("Write", {
            "file_path": "settings.py",
            "content": 'api_key = "sk_live_' + "a" * 24 + '"',
        })
        assert r is not None

    def test_blocks_password_assignment(self):
        r = check("Write", {
            "file_path": "config.py",
            "content": 'password = "SuperSecret123!@#"',
        })
        assert r is not None
        assert "Password Assignment" in r["secrets"]

    def test_blocks_stripe_key(self):
        r = check("Write", {
            "file_path": "pay.py",
            "content": 'sk_live_' + "a" * 22,
        })
        assert r is not None
        assert "Stripe Key" in r["secrets"]

    def test_blocks_openai_key(self):
        r = check("Write", {
            "file_path": "ai.py",
            "content": 'key = "sk-' + "a" * 48 + '"',
        })
        assert r is not None

    # ── Should pass ───────────────────────────────────

    def test_non_write_tool(self):
        assert check("Read", {"file_path": "x.py"}) is None

    def test_clean_code(self):
        assert check("Write", {
            "file_path": "main.py",
            "content": "def hello():\n    print('world')",
        }) is None

    def test_env_example_exempt(self):
        assert check("Write", {
            "file_path": "config/.env.example",
            "content": 'API_KEY = "AKIAIOSFODNN7EXAMPLE"',
        }) is None

    def test_readme_exempt(self):
        assert check("Write", {
            "file_path": "README.md",
            "content": 'Use your key: AKIAIOSFODNN7EXAMPLE',
        }) is None

    def test_empty_content(self):
        assert check("Write", {"file_path": "x.py", "content": ""}) is None

    def test_non_dict_input(self):
        assert check("Write", "not a dict") is None

    def test_env_var_reference_ok(self):
        assert check("Write", {
            "file_path": "main.py",
            "content": 'key = os.environ["API_KEY"]',
        }) is None

    # ── Extra patterns ────────────────────────────────

    def test_extra_patterns(self):
        import re

        custom = [("Custom Token", re.compile(r"MYAPP_[A-Z]{20}"))]
        r = check("Write", {
            "file_path": "x.py",
            "content": "MYAPP_ABCDEFGHIJKLMNOPQRST",
        }, extra_patterns=custom)
        assert r is not None
        assert "Custom Token" in r["secrets"]

    # ── Deny message ─────────────────────────────────

    def test_deny_message_contains_filename(self):
        r = check("Write", {
            "file_path": "/path/to/config.py",
            "content": 'password = "hunter2abc"',
        })
        assert "config.py" in r["reason"]
