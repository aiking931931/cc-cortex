"""Tests for concinno.publish_scan — Pre-publish artifact scanner."""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import zipfile

from concinno.publish_scan import (
    _check_content,
    _check_filename,
    check_breaking_changes,
    extract_public_api,
    save_api_snapshot,
    scan_dist,
    scan_dist_summary,
    scan_file,
    semver_gate,
)

# ── Helpers ──────────────────────────────────────────────────

def _make_wheel(tmp_dir: str, files: dict[str, str], name: str = "pkg-1.0.0.whl") -> str:
    """Create a minimal .whl (zip) with given {path: content} entries."""
    path = os.path.join(tmp_dir, name)
    with zipfile.ZipFile(path, "w") as zf:
        for fname, content in files.items():
            zf.writestr(fname, content)
    return path


def _make_sdist(tmp_dir: str, files: dict[str, str], name: str = "pkg-1.0.0.tar.gz") -> str:
    """Create a minimal .tar.gz with given {path: content} entries."""
    path = os.path.join(tmp_dir, name)
    with tarfile.open(path, "w:gz") as tf:
        for fname, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=fname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


# ── _check_filename ──────────────────────────────────────────

class TestCheckFilename:
    def test_env_file_critical(self):
        r = _check_filename("pkg/.env")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_env_local_critical(self):
        r = _check_filename("pkg/.env.local")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_id_rsa_critical(self):
        r = _check_filename("keys/id_rsa")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_pem_extension_critical(self):
        r = _check_filename("certs/server.pem")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_p12_extension_critical(self):
        r = _check_filename("cert.p12")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_pypirc_critical(self):
        r = _check_filename("home/.pypirc")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_suspicious_secret_json_high(self):
        r = _check_filename("config/my_secret_keys.json")
        assert r is not None
        assert r["severity"] == "HIGH"

    def test_credentials_json_critical(self):
        r = _check_filename("config/credentials.json")
        assert r is not None
        assert r["severity"] == "CRITICAL"

    def test_normal_py_ok(self):
        assert _check_filename("pkg/main.py") is None

    def test_normal_init_ok(self):
        assert _check_filename("pkg/__init__.py") is None

    def test_readme_ok(self):
        assert _check_filename("README.md") is None

    def test_setup_cfg_ok(self):
        assert _check_filename("setup.cfg") is None


# ── _check_content ───────────────────────────────────────────

class TestCheckContent:
    def test_aws_key_detected(self):
        content = 'aws_key = "AKIAIOSFODNN7EXAMPLE"'
        issues = _check_content("config.py", content)
        assert any(i["pattern"] == "secret" for i in issues)

    def test_private_key_pem(self):
        content = "-----BEGIN RSA PRIVATE KEY-----\ndata\n-----END RSA PRIVATE KEY-----"
        issues = _check_content("key.py", content)
        assert any("Private Key" in i["reason"] for i in issues)

    def test_github_token(self):
        content = 'token = "ghp_' + "A" * 36 + '"'
        issues = _check_content("auth.py", content)
        assert any("GitHub" in i["reason"] for i in issues)

    def test_personal_path_windows(self):
        content = 'path = r"C:\\Users\\john\\projects\\secret"'
        issues = _check_content("config.py", content)
        assert any(i["pattern"] == "personal_path" for i in issues)

    def test_personal_path_linux(self):
        content = 'path = "/home/john/work/project"'
        issues = _check_content("config.py", content)
        assert any(i["pattern"] == "personal_path" for i in issues)

    def test_personal_path_macos(self):
        content = 'path = "/Users/john/Desktop/code"'
        issues = _check_content("config.py", content)
        assert any(i["pattern"] == "personal_path" for i in issues)

    def test_clean_content_ok(self):
        content = "def hello():\n    return 'world'\n"
        assert _check_content("main.py", content) == []

    def test_stripe_key(self):
        content = 'STRIPE_KEY = "sk_live_' + "a" * 24 + '"'
        issues = _check_content("billing.py", content)
        assert any("Stripe" in i["reason"] for i in issues)

    def test_anthropic_key(self):
        content = 'key = "sk-ant-api03-' + "A" * 85 + '"'
        issues = _check_content("llm.py", content)
        assert any("Anthropic" in i["reason"] for i in issues)


# ── scan_file (wheel) ────────────────────────────────────────

class TestScanWheel:
    def test_clean_wheel_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            whl = _make_wheel(tmp, {"pkg/__init__.py": "# ok\n"})
            assert scan_file(whl) == []

    def test_detects_env_in_wheel(self):
        with tempfile.TemporaryDirectory() as tmp:
            whl = _make_wheel(tmp, {
                "pkg/__init__.py": "# ok\n",
                "pkg/.env": "SECRET=oops\n",
            })
            issues = scan_file(whl)
            assert any(i["severity"] == "CRITICAL" for i in issues)

    def test_detects_secret_in_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            whl = _make_wheel(tmp, {
                "pkg/config.py": 'TOKEN = "ghp_' + "A" * 40 + '"\n',
            })
            issues = scan_file(whl)
            assert any("GitHub" in i["reason"] for i in issues)

    def test_detects_private_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            whl = _make_wheel(tmp, {
                "pkg/server.key": "not really a key\n",
            })
            issues = scan_file(whl)
            assert any(i["severity"] == "CRITICAL" for i in issues)

    def test_bad_zip_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.whl")
            with open(bad, "w") as f:
                f.write("not a zip")
            issues = scan_file(bad)
            assert any(i["pattern"] == "io_error" for i in issues)


# ── scan_file (sdist) ────────────────────────────────────────

class TestScanSdist:
    def test_clean_sdist_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = _make_sdist(tmp, {"pkg-1.0.0/pkg/__init__.py": "# ok\n"})
            assert scan_file(sd) == []

    def test_detects_id_rsa_in_sdist(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = _make_sdist(tmp, {
                "pkg-1.0.0/pkg/__init__.py": "# ok\n",
                "pkg-1.0.0/id_rsa": "private key data\n",
            })
            issues = scan_file(sd)
            assert any(i["severity"] == "CRITICAL" for i in issues)

    def test_detects_personal_path_in_sdist(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = _make_sdist(tmp, {
                "pkg-1.0.0/pkg/config.py": 'BASE = "C:\\Users\\dev\\project"\n',
            })
            issues = scan_file(sd)
            assert any(i["pattern"] == "personal_path" for i in issues)


# ── scan_dist (directory) ────────────────────────────────────

class TestScanDist:
    def test_missing_dir_returns_error(self):
        issues = scan_dist("/nonexistent/path/dist")
        assert any(i["pattern"] == "no_dist" for i in issues)

    def test_empty_dist_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            issues = scan_dist(dist)
            assert any("No distribution" in i["reason"] for i in issues)

    def test_clean_dist_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            _make_wheel(dist, {"pkg/__init__.py": "# ok\n"})
            assert scan_dist(dist) == []

    def test_catches_issues_in_dist(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            _make_wheel(dist, {
                "pkg/__init__.py": "# ok\n",
                "pkg/.env": "OOPS=1\n",
            })
            issues = scan_dist(dist)
            assert len(issues) > 0

    def test_scans_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            _make_wheel(dist, {"pkg/__init__.py": "# ok\n"}, name="a-1.0.whl")
            _make_sdist(dist, {"a-1.0/pkg/__init__.py": "# ok\n"}, name="a-1.0.tar.gz")
            assert scan_dist(dist) == []


# ── scan_dist_summary ────────────────────────────────────────

class TestScanDistSummary:
    def test_clean_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            _make_wheel(dist, {"pkg/__init__.py": "# ok\n"})
            s = scan_dist_summary(dist)
            assert "clean" in s
            assert "✅" in s

    def test_dirty_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            _make_wheel(dist, {"pkg/.env": "SECRET=x\n"})
            s = scan_dist_summary(dist)
            assert "CRITICAL" in s
            assert "🚨" in s


# ── Unknown format ───────────────────────────────────────────

class TestUnknownFormat:
    def test_unknown_extension_warns(self):
        issues = scan_file("something.rpm")
        assert any(i["severity"] == "WARN" for i in issues)


# ── Semver Breaking Change Gate ─────────────────────────────


class TestExtractPublicApi:
    def test_extracts_functions_and_classes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pkg")
            os.makedirs(src)
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write(
                    "def hello(): pass\n"
                    "def _private(): pass\n"
                    "class Foo: pass\n"
                    "class _Bar: pass\n"
                    "VERSION = '1.0'\n"
                    "_INTERNAL = 42\n"
                )
            api = extract_public_api(src)
            names = api.get("mod.py", set())
            assert "def hello()" in names
            assert "class Foo" in names
            assert "var VERSION" in names
            # Private names excluded
            assert not any(n.startswith("def _") for n in names)
            assert "class _Bar" not in names
            assert "var _INTERNAL" not in names

    def test_nonexistent_dir_returns_empty(self):
        assert extract_public_api("/nonexistent") == {}


class TestCheckBreakingChanges:
    def test_no_changes_clean(self):
        old = {"mod.py": {"def hello", "class Foo"}}
        new = {"mod.py": {"def hello", "class Foo"}}
        assert check_breaking_changes(old, new) == []

    def test_addition_is_not_breaking(self):
        old = {"mod.py": {"def hello"}}
        new = {"mod.py": {"def hello", "def world"}}
        assert check_breaking_changes(old, new) == []

    def test_removal_is_breaking(self):
        old = {"mod.py": {"def hello", "def removed_fn"}}
        new = {"mod.py": {"def hello"}}
        issues = check_breaking_changes(old, new)
        assert len(issues) == 1
        assert issues[0]["severity"] == "CRITICAL"
        assert "removed_fn" in issues[0]["reason"]

    def test_module_removal_is_breaking(self):
        old = {"mod.py": {"def hello"}, "gone.py": {"def bye"}}
        new = {"mod.py": {"def hello"}}
        issues = check_breaking_changes(old, new)
        assert any("Module removed" in i["reason"] for i in issues)

    def test_rename_detected_as_remove_plus_add(self):
        old = {"mod.py": {"def old_name"}}
        new = {"mod.py": {"def new_name"}}
        issues = check_breaking_changes(old, new)
        assert len(issues) == 1
        assert "old_name" in issues[0]["reason"]


class TestSemverGate:
    def test_clean_when_no_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pkg")
            os.makedirs(src)
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write("def hello(): pass\n")
            assert semver_gate(src) == []

    def test_detects_breaking_with_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pkg")
            os.makedirs(src)
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write("def hello(): pass\n")

            snap = os.path.join(tmp, "api.json")
            save_api_snapshot(src, snap)

            # Remove function → breaking
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write("def different(): pass\n")

            issues = semver_gate(src, old_api_file=snap)
            assert any(i["pattern"] == "api_removed" for i in issues)

    def test_no_breaking_when_only_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pkg")
            os.makedirs(src)
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write("def hello(): pass\n")

            snap = os.path.join(tmp, "api.json")
            save_api_snapshot(src, snap)

            # Add function → not breaking
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write("def hello(): pass\ndef world(): pass\n")

            assert semver_gate(src, old_api_file=snap) == []


class TestSaveApiSnapshot:
    def test_saves_and_loads(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pkg")
            os.makedirs(src)
            with open(os.path.join(src, "mod.py"), "w") as f:
                f.write("def hello(): pass\nVAR = 1\n")

            snap = os.path.join(tmp, "api.json")
            save_api_snapshot(src, snap)

            with open(snap) as f:
                data = json.load(f)
            assert "mod.py" in data
            assert "def hello()" in data["mod.py"]
            assert "var VAR" in data["mod.py"]
