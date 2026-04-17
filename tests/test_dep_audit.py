"""Tests for concinno.dep_audit — Typosquatting + scope spoofing + blocklist + poisoning."""

from __future__ import annotations

from concinno.dep_audit import check, check_poisoning


class TestDepAuditCheck:
    # ── Should block ──────────────────────────────────

    def test_blocks_pip_typosquat(self):
        r = check("Bash", {"command": "pip install reqeusts"})
        assert r is not None
        assert r["permissionDecision"] == "deny"
        assert r["typosquat"] == "reqeusts"
        assert r["real_package"] == "requests"

    def test_blocks_pip3_typosquat(self):
        r = check("Bash", {"command": "pip3 install djago"})
        assert r is not None
        assert r["real_package"] == "django"

    def test_blocks_npm_typosquat(self):
        r = check("Bash", {"command": "npm install lodahs"})
        assert r is not None
        assert r["real_package"] == "lodash"

    def test_blocks_yarn_typosquat(self):
        r = check("Bash", {"command": "yarn add axois"})
        assert r is not None
        assert r["real_package"] == "axios"

    def test_blocks_uv_typosquat(self):
        r = check("Bash", {"command": "uv pip install fask"})
        assert r is not None
        assert r["real_package"] == "flask"

    def test_blocks_python_m_pip(self):
        r = check("Bash", {"command": "python -m pip install numppy"})
        assert r is not None
        assert r["real_package"] == "numpy"

    def test_blocks_pnpm_typosquat(self):
        r = check("Bash", {"command": "pnpm add recat"})
        assert r is not None
        assert r["real_package"] == "react"

    # ── Should pass ───────────────────────────────────

    def test_correct_package_ok(self):
        assert check("Bash", {"command": "pip install requests"}) is None

    def test_correct_npm_ok(self):
        assert check("Bash", {"command": "npm install lodash"}) is None

    def test_non_install_command(self):
        assert check("Bash", {"command": "pip freeze"}) is None

    def test_non_bash_tool(self):
        assert check("Read", {"file_path": "x"}) is None

    def test_empty_command(self):
        assert check("Bash", {"command": ""}) is None

    def test_non_dict_input(self):
        assert check("Bash", "not a dict") is None

    def test_flags_ignored(self):
        assert check("Bash", {"command": "pip install --upgrade requests"}) is None

    # ── Extra patterns ────────────────────────────────

    def test_extra_typosquats(self):
        r = check("Bash", {
            "command": "pip install my-internal-pkg-typo",
        }, extra_typosquats={"my-internal-pkg-typo": "my-internal-pkg"})
        assert r is not None
        assert r["real_package"] == "my-internal-pkg"

    # ── Deny message ─────────────────────────────────

    def test_deny_message_helpful(self):
        r = check("Bash", {"command": "pip install reqeusts"})
        assert "requests" in r["reason"]
        assert "typo" in r["reason"].lower() or "Typosquatting" in r["reason"]


class TestBlocklist:
    """Known malicious package blocklist."""

    def test_blocks_known_malicious_pip(self):
        r = check("Bash", {"command": "pip install colourama"})
        assert r is not None
        assert r["permissionDecision"] == "deny"
        assert r["check_type"] == "blocklist"
        assert r["blocked_package"] == "colourama"

    def test_blocks_known_malicious_npm(self):
        r = check("Bash", {"command": "npm install crossenv"})
        assert r is not None
        assert r["check_type"] == "blocklist"

    def test_blocks_ctx(self):
        r = check("Bash", {"command": "pip install ctx"})
        assert r is not None
        assert r["check_type"] == "blocklist"

    def test_blocks_event_stream(self):
        r = check("Bash", {"command": "npm install event-stream"})
        assert r is not None
        assert r["check_type"] == "blocklist"

    def test_blocks_with_version_specifier(self):
        r = check("Bash", {"command": "pip install colourama>=1.0"})
        assert r is not None
        assert r["check_type"] == "blocklist"

    def test_safe_package_not_blocked(self):
        assert check("Bash", {"command": "pip install colorama"}) is None

    def test_extra_blocklist(self):
        r = check(
            "Bash",
            {"command": "pip install evil-internal-pkg"},
            extra_blocklist={"evil-internal-pkg"},
        )
        assert r is not None
        assert r["check_type"] == "blocklist"
        assert "custom blocklist" in r["reason"]

    def test_extra_blocklist_safe(self):
        assert check(
            "Bash",
            {"command": "pip install good-pkg"},
            extra_blocklist={"evil-internal-pkg"},
        ) is None

    def test_blocklist_priority_over_typosquat(self):
        """Blocklist should take priority when a package is both blocked and a typosquat."""
        r = check("Bash", {"command": "pip install mongose"})
        assert r is not None
        # mongose is in blocklist — should match blocklist first
        assert r["check_type"] == "blocklist"


class TestScopeSpoofing:
    """npm scope typosquatting detection."""

    def test_angular_typo(self):
        r = check("Bash", {"command": "npm install @angualr/core"})
        assert r is not None
        assert r["check_type"] == "scope_spoof"
        assert r["real_scope"] == "@angular"

    def test_babel_typo(self):
        r = check("Bash", {"command": "yarn add @bable/core"})
        assert r is not None
        assert r["check_type"] == "scope_spoof"
        assert r["real_scope"] == "@babel"

    def test_types_typo(self):
        r = check("Bash", {"command": "npm install @tpyes/node"})
        assert r is not None
        assert r["check_type"] == "scope_spoof"

    def test_anthropic_typo(self):
        r = check("Bash", {"command": "npm install @anthropic/sdk"})
        assert r is not None
        assert r["check_type"] == "scope_spoof"
        assert r["real_scope"] == "@anthropic-ai"

    def test_legit_angular_ok(self):
        assert check("Bash", {"command": "npm install @angular/core"}) is None

    def test_legit_types_ok(self):
        assert check("Bash", {"command": "npm install @types/node"}) is None

    def test_legit_babel_ok(self):
        assert check("Bash", {"command": "yarn add @babel/core"}) is None

    def test_non_scoped_unaffected(self):
        assert check("Bash", {"command": "npm install express"}) is None

    def test_scope_with_version(self):
        r = check("Bash", {"command": "npm install @angualr/core@15.0.0"})
        assert r is not None
        assert r["check_type"] == "scope_spoof"


class TestSupplyChainPoisoning:
    """Supply-chain poisoning detection (LiteLLM/Apifox 2026-03 vectors)."""

    def test_pth_site_packages_write(self):
        r = check_poisoning("Bash", {
            "command": "cp evil.pth /usr/lib/python3/dist-packages/evil.pth"
        })
        assert r is not None
        assert r["check_type"] == "pth_injection"

    def test_pth_write_tool(self):
        r = check_poisoning("Write", {
            "file_path": "/tmp/something.pth"
        })
        assert r is not None
        assert r["check_type"] == "pth_write"

    def test_pth_edit_tool(self):
        r = check_poisoning("Edit", {
            "file_path": "/home/user/.local/lib/python3.11/site-packages/evil.pth"
        })
        assert r is not None
        assert r["check_type"] == "pth_write"

    def test_normal_pip_install_clean(self):
        r = check_poisoning("Bash", {
            "command": "pip install requests"
        })
        assert r is None

    def test_normal_write_clean(self):
        r = check_poisoning("Write", {
            "file_path": "/home/user/project/main.py"
        })
        assert r is None

    def test_non_bash_tool_clean(self):
        r = check_poisoning("Read", {"file_path": "/etc/passwd"})
        assert r is None

    def test_pth_in_normal_path_no_site_packages(self):
        """pth in Bash but not in site-packages → no alert for read."""
        r = check_poisoning("Bash", {
            "command": "cat /tmp/test.pth"
        })
        assert r is None

    def test_echo_to_pth_detected(self):
        """echo/redirect to .pth file → detected."""
        r = check_poisoning("Bash", {
            "command": "echo 'import evil' > /tmp/evil.pth"
        })
        assert r is not None
        assert r["check_type"] == "pth_write_bash"

    def test_editable_install_untrusted(self):
        """pip install -e from untrusted path → detected."""
        r = check_poisoning("Bash", {
            "command": "pip install -e /tmp/malicious-pkg"
        })
        assert r is not None
        assert r["check_type"] == "untrusted_editable"

    def test_editable_install_current_dir_ok(self):
        """pip install -e . → safe."""
        r = check_poisoning("Bash", {
            "command": "pip install -e ."
        })
        assert r is None

    def test_editable_install_dot_extras_ok(self):
        """pip install -e .[dev] → safe."""
        r = check_poisoning("Bash", {
            "command": "pip install -e .[dev]"
        })
        assert r is None

    def test_pyproject_build_hook_detected(self):
        """Writing suspicious cmdclass to pyproject.toml → detected."""
        r = check_poisoning("Write", {
            "file_path": "/project/pyproject.toml",
            "content": "[tool.setuptools.cmdclass]\ninstall = 'evil:Install'"
        })
        assert r is not None
        assert r["check_type"] == "build_hook"

    def test_pyproject_normal_ok(self):
        """Normal pyproject.toml write → no alert."""
        r = check_poisoning("Write", {
            "file_path": "/project/pyproject.toml",
            "content": "[project]\nname = 'mylib'\nversion = '1.0'"
        })
        assert r is None
