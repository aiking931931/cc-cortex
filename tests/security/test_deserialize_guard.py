"""Tests for concinno.security.deserialize_guard.

Coverage targets (≥80 cases):
  * 5+ negative + 10+ positive cases per detector pattern
    (pickle / yaml / dill / marshal / eval-exec / __reduce__ / Popen)
  * yaml.load Loader-aware safe / unsafe distinction
  * pickle.load(file) and pickle.loads(bytes) both flagged
  * eval / exec literal vs variable severity split
  * Per-line escape-hatch comment recognised
  * Profile fail-mode resolution (lite / mainstream / strict / paranoid)
  * Audit log written for warn+log + hard_deny only
  * ZIQ outcome bus emit lifecycle
  * Regression: AST parse error handled gracefully
  * trusted_modules allow-list suppresses findings
  * Test-fixture path detection downgrades severity
  * malformed_payload for non-source dict / unsupported type
  * Constructor parameter validation
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from concinno.security import (
    DeserializeFinding,
    DeserializeGuard,
    PolicyGate,
)
from concinno.security import deserialize_guard as dg_mod

# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    """Redirect audit writes + disable ZIQ bus for clean isolation."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


def _types(findings: list[DeserializeFinding]) -> set[str]:
    return {f.type for f in findings}


def _scan(src: str, **kwargs: Any) -> list[DeserializeFinding]:
    """Run scan on a literal source string with default min_severity=low."""
    g = DeserializeGuard(min_severity="low", **kwargs)
    return g.scan(src)


# ══════════════════════════════════════════════════════════════
#  1. Inheritance / class invariants
# ══════════════════════════════════════════════════════════════


def test_inherits_from_policygate(audit_tmp: Path) -> None:
    assert issubclass(DeserializeGuard, PolicyGate)


def test_class_name_constant() -> None:
    assert DeserializeGuard.name == "deserialize_guard"


def test_invalid_min_severity_raises() -> None:
    with pytest.raises(ValueError):
        DeserializeGuard(min_severity="extreme")  # type: ignore[arg-type]


def test_default_trusted_modules_empty() -> None:
    g = DeserializeGuard()
    assert g._trusted_modules == frozenset()


def test_pattern_severity_overrides_apply(audit_tmp: Path) -> None:
    g = DeserializeGuard(
        min_severity="low",
        pattern_severity_overrides={"pickle.loads": "low"},
    )
    findings = g.scan("import pickle\npickle.loads(b'')")
    assert findings
    assert findings[0].severity == "low"


# ══════════════════════════════════════════════════════════════
#  2. pickle detector — ≥10 positive + ≥5 negative
# ══════════════════════════════════════════════════════════════


_PICKLE_POSITIVES = [
    "import pickle\npickle.loads(b'data')",
    "import pickle\npickle.load(open('x', 'rb'))",
    "import pickle as p\np.loads(buf)",  # alias not resolved → won't fire
    "import pickle\nx = pickle.loads(payload)",
    "import pickle\nresult = pickle.load(fp)",
    "from pickle import loads\nloads(b'')",  # bare name not resolved → won't fire
    "import pickle\nfor i in range(3): pickle.loads(items[i])",
    "import pickle\ndef foo(): return pickle.loads(b)",
    "import pickle\ntry: pickle.load(f)\nexcept Exception: pass",
    "import pickle\nclass X: data = pickle.loads(b'')",
    "import pickle\npickle.loads(b'a'); pickle.loads(b'b')",
    "import pickle\nresults = [pickle.loads(b) for b in blobs]",
]

# Of the above, only those that resolve to literal `pickle.load` / `pickle.loads`
# qualified-name fire. Aliases and from-imports are NOT in scope (documented
# limitation — name resolution would require a symbol table).
_PICKLE_QNAME_FIRING = [
    s for s in _PICKLE_POSITIVES
    if "pickle.loads" in s or "pickle.load(" in s
]


@pytest.mark.parametrize("source", _PICKLE_QNAME_FIRING)
def test_pickle_positive_fires(audit_tmp: Path, source: str) -> None:
    findings = _scan(source)
    assert any(
        f.type in {"pickle.load", "pickle.loads"} for f in findings
    ), f"missing pickle finding for: {source!r}"


def test_pickle_alias_not_resolved(audit_tmp: Path) -> None:
    """Documented limitation: ``import pickle as p`` is out of scope."""
    findings = _scan("import pickle as p\np.loads(b'')")
    assert not any(f.type.startswith("pickle.") for f in findings)


def test_pickle_from_import_not_resolved(audit_tmp: Path) -> None:
    """Documented limitation: ``from pickle import loads`` is out of scope."""
    findings = _scan("from pickle import loads\nloads(b'')")
    assert not any(f.type.startswith("pickle.") for f in findings)


_PICKLE_NEGATIVES = [
    "import json\njson.loads(b'{}')",
    "x = 'pickle.loads(...)'",  # string literal mention only
    "# pickle.loads(b'') in a comment",
    "import pickle\npickle.HIGHEST_PROTOCOL",  # attr access, no call
    "import pickle\npickle.Pickler(buf)",  # different API
    "loads(b'')",  # bare call w/o module
]


@pytest.mark.parametrize("source", _PICKLE_NEGATIVES)
def test_pickle_negative(audit_tmp: Path, source: str) -> None:
    findings = _scan(source)
    assert not any(
        f.type in {"pickle.load", "pickle.loads"} for f in findings
    ), f"unexpected pickle finding for: {source!r}"


def test_pickle_severity_critical_default() -> None:
    findings = _scan("import pickle\npickle.loads(b'')")
    assert findings[0].severity == "critical"


def test_pickle_allow_with_protocol_suppresses(audit_tmp: Path) -> None:
    findings = _scan(
        "import pickle\npickle.loads(b'')",
        allow_pickle_with_protocol=True,
    )
    assert not any(f.type.startswith("pickle.") for f in findings)


def test_pickle_load_file_object(audit_tmp: Path) -> None:
    findings = _scan("import pickle\npickle.load(open('x', 'rb'))")
    types = _types(findings)
    assert "pickle.load" in types


def test_pickle_loads_bytes(audit_tmp: Path) -> None:
    findings = _scan("import pickle\npickle.loads(b'\\x80')")
    types = _types(findings)
    assert "pickle.loads" in types


# ══════════════════════════════════════════════════════════════
#  3. yaml detector — Loader-aware
# ══════════════════════════════════════════════════════════════


def test_yaml_load_no_loader_critical(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream)")
    assert any(f.type == "yaml.load" for f in findings)


def test_yaml_load_with_unsafe_loader_critical(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream, Loader=yaml.FullLoader)")
    assert any(f.type == "yaml.load" for f in findings)


def test_yaml_load_with_safe_loader_not_flagged(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream, Loader=yaml.SafeLoader)")
    assert not any(f.type.startswith("yaml.") for f in findings)


def test_yaml_load_with_csafeloader_not_flagged(audit_tmp: Path) -> None:
    findings = _scan(
        "import yaml\nyaml.load(stream, Loader=yaml.CSafeLoader)"
    )
    assert not any(f.type.startswith("yaml.") for f in findings)


def test_yaml_load_with_baseloader_not_flagged(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream, Loader=yaml.BaseLoader)")
    assert not any(f.type.startswith("yaml.") for f in findings)


def test_yaml_safe_load_not_flagged(audit_tmp: Path) -> None:
    """``yaml.safe_load(stream)`` is out of scope — different qname."""
    findings = _scan("import yaml\nyaml.safe_load(stream)")
    assert not any(f.type.startswith("yaml.") for f in findings)


def test_yaml_unsafe_load_flagged(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.unsafe_load(stream)")
    assert any(f.type == "yaml.unsafe_load" for f in findings)


def test_yaml_load_positional_safe_loader(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream, yaml.SafeLoader)")
    assert not any(f.type.startswith("yaml.") for f in findings)


def test_yaml_load_positional_full_loader(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream, yaml.FullLoader)")
    assert any(f.type == "yaml.load" for f in findings)


def test_yaml_strict_mode_can_be_disabled(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low", yaml_safe_loader_only=False)
    findings = g.scan("import yaml\nyaml.load(stream)")
    assert not any(f.type == "yaml.load" for f in findings)


def test_yaml_severity_critical(audit_tmp: Path) -> None:
    findings = _scan("import yaml\nyaml.load(stream)")
    yaml_finds = [f for f in findings if f.type == "yaml.load"]
    assert yaml_finds[0].severity == "critical"


def test_yaml_load_in_loop(audit_tmp: Path) -> None:
    src = (
        "import yaml\n"
        "for f in files:\n"
        "    yaml.load(f)\n"
    )
    findings = _scan(src)
    assert any(f.type == "yaml.load" for f in findings)


# ══════════════════════════════════════════════════════════════
#  4. dill detector
# ══════════════════════════════════════════════════════════════


def test_dill_loads_flagged(audit_tmp: Path) -> None:
    findings = _scan("import dill\ndill.loads(b'')")
    assert any(f.type == "dill.loads" for f in findings)


def test_dill_load_flagged(audit_tmp: Path) -> None:
    findings = _scan("import dill\ndill.load(f)")
    assert any(f.type == "dill.load" for f in findings)


def test_dill_severity_high(audit_tmp: Path) -> None:
    findings = _scan("import dill\ndill.loads(b'')")
    dills = [f for f in findings if f.type.startswith("dill.")]
    assert dills[0].severity == "high"


def test_dill_dump_not_flagged(audit_tmp: Path) -> None:
    """``dill.dump`` (serialise) is the reverse op — not in scope."""
    findings = _scan("import dill\ndill.dump(obj, f)")
    assert not any(f.type.startswith("dill.") for f in findings)


def test_dill_other_attr_not_flagged(audit_tmp: Path) -> None:
    findings = _scan("import dill\ndill.copy(obj)")
    assert not any(f.type.startswith("dill.") for f in findings)


# ══════════════════════════════════════════════════════════════
#  5. marshal detector
# ══════════════════════════════════════════════════════════════


def test_marshal_loads_flagged(audit_tmp: Path) -> None:
    findings = _scan("import marshal\nmarshal.loads(b'')")
    assert any(f.type == "marshal.loads" for f in findings)


def test_marshal_load_flagged(audit_tmp: Path) -> None:
    findings = _scan("import marshal\nmarshal.load(f)")
    assert any(f.type == "marshal.load" for f in findings)


def test_marshal_severity_high(audit_tmp: Path) -> None:
    findings = _scan("import marshal\nmarshal.loads(b'')")
    marshals = [f for f in findings if f.type.startswith("marshal.")]
    assert marshals[0].severity == "high"


def test_marshal_dump_not_flagged(audit_tmp: Path) -> None:
    findings = _scan("import marshal\nmarshal.dump(obj, f)")
    assert not any(f.type.startswith("marshal.") for f in findings)


# ══════════════════════════════════════════════════════════════
#  6. eval / exec — literal vs dynamic
# ══════════════════════════════════════════════════════════════


def test_eval_dynamic_arg_critical(audit_tmp: Path) -> None:
    findings = _scan("eval(user_input)")
    assert any(f.type == "eval" and f.severity == "critical" for f in findings)


def test_eval_attribute_arg_critical(audit_tmp: Path) -> None:
    findings = _scan("eval(req.body)")
    assert any(f.type == "eval" and f.severity == "critical" for f in findings)


def test_eval_call_arg_critical(audit_tmp: Path) -> None:
    findings = _scan("eval(get_input())")
    assert any(f.type == "eval" and f.severity == "critical" for f in findings)


def test_eval_literal_string_low(audit_tmp: Path) -> None:
    findings = _scan("eval('1 + 2')")
    assert any(f.type == "eval.literal" and f.severity == "low" for f in findings)


def test_eval_literal_int_low(audit_tmp: Path) -> None:
    findings = _scan("eval(42)")
    assert any(f.type == "eval.literal" for f in findings)


def test_eval_literal_bytes_low(audit_tmp: Path) -> None:
    findings = _scan("eval(b'42')")
    assert any(f.type == "eval.literal" for f in findings)


def test_eval_no_args_flagged(audit_tmp: Path) -> None:
    findings = _scan("eval()")
    assert any(f.type == "eval" for f in findings)


def test_exec_dynamic_arg_critical(audit_tmp: Path) -> None:
    findings = _scan("exec(payload)")
    assert any(f.type == "exec" and f.severity == "critical" for f in findings)


def test_exec_literal_string_low(audit_tmp: Path) -> None:
    findings = _scan("exec('x = 1')")
    assert any(f.type == "exec.literal" for f in findings)


def test_eval_min_severity_filters_literal(audit_tmp: Path) -> None:
    """Default min_severity=medium drops eval.literal (severity=low)."""
    g = DeserializeGuard()  # default min_severity=medium
    findings = g.scan("eval('1 + 2')")
    assert not any(f.type == "eval.literal" for f in findings)


def test_eval_method_attribute_not_flagged(audit_tmp: Path) -> None:
    """``obj.eval(...)`` is a method call, not the builtin."""
    findings = _scan("obj.eval(thing)")
    assert not any(f.type in {"eval", "exec"} for f in findings)


def test_exec_method_attribute_not_flagged(audit_tmp: Path) -> None:
    findings = _scan("self.exec(thing)")
    assert not any(f.type in {"eval", "exec"} for f in findings)


def test_eval_fstring_constant_low(audit_tmp: Path) -> None:
    """f-string with no FormattedValue children is constant."""
    findings = _scan('eval(f"hello world")')
    assert any(f.type == "eval.literal" for f in findings)


def test_eval_tuple_of_constants_low(audit_tmp: Path) -> None:
    findings = _scan("eval((1, 2, 3))")
    assert any(f.type == "eval.literal" for f in findings)


# ══════════════════════════════════════════════════════════════
#  7. __reduce__ override
# ══════════════════════════════════════════════════════════════


def test_reduce_override_flagged(audit_tmp: Path) -> None:
    src = (
        "class Evil:\n"
        "    def __reduce__(self):\n"
        "        return (os.system, ('rm -rf /',))\n"
    )
    findings = _scan(src)
    assert any(f.type == "__reduce__" for f in findings)


def test_reduce_override_severity_low(audit_tmp: Path) -> None:
    src = "class X:\n    def __reduce__(self): return (a, b)\n"
    findings = _scan(src)
    finds = [f for f in findings if f.type == "__reduce__"]
    assert finds[0].severity == "low"


def test_reduce_override_disabled(audit_tmp: Path) -> None:
    src = "class X:\n    def __reduce__(self): return ()\n"
    g = DeserializeGuard(min_severity="low", flag_reduce_override=False)
    findings = g.scan(src)
    assert not any(f.type == "__reduce__" for f in findings)


def test_other_dunder_not_flagged(audit_tmp: Path) -> None:
    src = "class X:\n    def __init__(self): pass\n"
    findings = _scan(src)
    assert not any(f.type == "__reduce__" for f in findings)


def test_reduce_async_def_flagged(audit_tmp: Path) -> None:
    """Even though async __reduce__ is unusual, we still flag it."""
    src = "class X:\n    async def __reduce__(self): return ()\n"
    findings = _scan(src)
    assert any(f.type == "__reduce__" for f in findings)


# ══════════════════════════════════════════════════════════════
#  8. subprocess.Popen(shell=True)
# ══════════════════════════════════════════════════════════════


def test_popen_shell_true_flagged(audit_tmp: Path) -> None:
    findings = _scan(
        "import subprocess\nsubprocess.Popen(cmd, shell=True)"
    )
    assert any(f.type == "subprocess.Popen.shell" for f in findings)


def test_popen_shell_false_not_flagged(audit_tmp: Path) -> None:
    findings = _scan(
        "import subprocess\nsubprocess.Popen(cmd, shell=False)"
    )
    assert not any(f.type == "subprocess.Popen.shell" for f in findings)


def test_popen_no_shell_not_flagged(audit_tmp: Path) -> None:
    findings = _scan("import subprocess\nsubprocess.Popen(['ls', '-l'])")
    assert not any(f.type == "subprocess.Popen.shell" for f in findings)


def test_popen_severity_medium(audit_tmp: Path) -> None:
    findings = _scan(
        "import subprocess\nsubprocess.Popen(cmd, shell=True)"
    )
    finds = [f for f in findings if f.type == "subprocess.Popen.shell"]
    assert finds[0].severity == "medium"


def test_bare_popen_with_shell_true(audit_tmp: Path) -> None:
    """Bare ``Popen(cmd, shell=True)`` (post from-import) is also flagged."""
    findings = _scan(
        "from subprocess import Popen\nPopen(cmd, shell=True)"
    )
    assert any(f.type == "subprocess.Popen.shell" for f in findings)


def test_popen_shell_variable_not_flagged(audit_tmp: Path) -> None:
    """``shell=flag`` (variable) is too speculative — only literal True fires."""
    findings = _scan(
        "import subprocess\nsubprocess.Popen(cmd, shell=user_flag)"
    )
    assert not any(f.type == "subprocess.Popen.shell" for f in findings)


# ══════════════════════════════════════════════════════════════
#  9. Per-line escape comment
# ══════════════════════════════════════════════════════════════


def test_per_line_escape_suppresses_finding(audit_tmp: Path) -> None:
    src = (
        "import pickle\n"
        "pickle.loads(trusted)  # CONCINNO_DISABLE:deserialize_guard:test\n"
    )
    findings = _scan(src)
    assert not any(f.type.startswith("pickle.") for f in findings)


def test_per_line_escape_does_not_leak_to_other_lines(audit_tmp: Path) -> None:
    src = (
        "import pickle\n"
        "pickle.loads(trusted)  # CONCINNO_DISABLE:deserialize_guard:ok\n"
        "pickle.loads(untrusted)\n"
    )
    findings = _scan(src)
    pickle_finds = [f for f in findings if f.type.startswith("pickle.")]
    assert len(pickle_finds) == 1


def test_per_line_escape_case_insensitive(audit_tmp: Path) -> None:
    src = (
        "import pickle\n"
        "pickle.loads(x)  # concinno_disable:DESERIALIZE_GUARD:why\n"
    )
    findings = _scan(src)
    assert not any(f.type.startswith("pickle.") for f in findings)


def test_global_escape_suppresses_all(audit_tmp: Path) -> None:
    """Base-class ``# CONCINNO_DISABLE:`` short-circuits the entire scan."""
    src = (
        "# CONCINNO_DISABLE: legacy module\n"
        "import pickle\npickle.loads(b'')\n"
        "exec(payload)\n"
    )
    g = DeserializeGuard(min_severity="low", fail_mode_override="hard_deny")
    result = g.evaluate(src)
    assert result.escaped is True
    assert result.decision == "accept"


# ══════════════════════════════════════════════════════════════
#  10. Trusted-module allow-list
# ══════════════════════════════════════════════════════════════


def test_trusted_module_suppresses(audit_tmp: Path) -> None:
    """``trusted_modules={"pickle"}`` silences pickle.* qnames."""
    g = DeserializeGuard(
        min_severity="low",
        trusted_modules=frozenset({"pickle"}),
    )
    findings = g.scan("import pickle\npickle.loads(b'')")
    assert not any(f.type.startswith("pickle.") for f in findings)


def test_trusted_module_prefix_match(audit_tmp: Path) -> None:
    g = DeserializeGuard(
        min_severity="low",
        trusted_modules=frozenset({"yaml"}),
    )
    findings = g.scan("import yaml\nyaml.load(stream)")
    assert not any(f.type.startswith("yaml.") for f in findings)


def test_trusted_module_unrelated_does_not_suppress(audit_tmp: Path) -> None:
    g = DeserializeGuard(
        min_severity="low",
        trusted_modules=frozenset({"safelib"}),
    )
    findings = g.scan("import pickle\npickle.loads(b'')")
    assert any(f.type.startswith("pickle.") for f in findings)


def test_trusted_module_set_input_accepted(audit_tmp: Path) -> None:
    """Constructor accepts ``set`` (not just ``frozenset``)."""
    g = DeserializeGuard(min_severity="low", trusted_modules={"pickle"})
    findings = g.scan("import pickle\npickle.loads(b'')")
    assert not findings


# ══════════════════════════════════════════════════════════════
#  11. Test-fixture path downgrade
# ══════════════════════════════════════════════════════════════


def test_fixture_path_downgrades_severity(
    audit_tmp: Path, tmp_path: Path,
) -> None:
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    src = "import pickle\npickle.loads(b'')\n"
    target = fixtures / "fixture.py"
    target.write_text(src, encoding="utf-8")

    g = DeserializeGuard(min_severity="low")
    result = g.evaluate(target)
    pickle_finds = [f for f in result.findings if f.type.startswith("pickle.")]
    assert pickle_finds
    # critical → high after downgrade.
    assert pickle_finds[0].severity == "high"


def test_non_fixture_path_keeps_severity(
    audit_tmp: Path, tmp_path: Path,
) -> None:
    src_dir = tmp_path / "src" / "myapp"
    src_dir.mkdir(parents=True)
    src = "import pickle\npickle.loads(b'')\n"
    target = src_dir / "module.py"
    target.write_text(src, encoding="utf-8")

    g = DeserializeGuard(min_severity="low")
    result = g.evaluate(target)
    pickle_finds = [f for f in result.findings if f.type.startswith("pickle.")]
    assert pickle_finds
    assert pickle_finds[0].severity == "critical"


def test_test_fixture_downgrade_does_not_below_low(
    audit_tmp: Path, tmp_path: Path,
) -> None:
    fixtures = tmp_path / "tests"
    fixtures.mkdir()
    target = fixtures / "case.py"
    target.write_text(
        "class X:\n    def __reduce__(self): return ()\n",
        encoding="utf-8",
    )
    g = DeserializeGuard(min_severity="low")
    result = g.evaluate(target)
    reduce_finds = [f for f in result.findings if f.type == "__reduce__"]
    assert reduce_finds
    assert reduce_finds[0].severity == "low"


# ══════════════════════════════════════════════════════════════
#  12. Profile fail-mode resolution
# ══════════════════════════════════════════════════════════════


def test_profile_lite_silent(audit_tmp: Path) -> None:
    g = DeserializeGuard(profile="lite", min_severity="low")
    result = g.evaluate("import pickle\npickle.loads(b'')")
    # lite default is silent for unregistered features.
    assert result.fail_mode == "silent"
    assert result.decision == "accept"


def test_profile_strict_hard_deny(audit_tmp: Path) -> None:
    g = DeserializeGuard(profile="strict", min_severity="low")
    result = g.evaluate("import pickle\npickle.loads(b'')")
    # strict pins deserialize_guard to hard_deny.
    assert result.fail_mode == "hard_deny"
    assert result.decision == "deny"


def test_profile_paranoid_hard_deny(audit_tmp: Path) -> None:
    g = DeserializeGuard(profile="paranoid", min_severity="low")
    result = g.evaluate("import pickle\npickle.loads(b'')")
    assert result.fail_mode == "hard_deny"
    assert result.decision == "deny"


def test_profile_mainstream_warn_default(audit_tmp: Path) -> None:
    g = DeserializeGuard(profile="mainstream", min_severity="low")
    result = g.evaluate("import pickle\npickle.loads(b'')")
    # mainstream default is warn for unpinned guards (deserialize_guard
    # has no override → falls through to fail_mode_default=warn).
    assert result.fail_mode == "warn"
    assert result.decision == "warn"


def test_constructor_override_beats_profile(audit_tmp: Path) -> None:
    g = DeserializeGuard(
        profile="paranoid",
        fail_mode_override="silent",
        min_severity="low",
    )
    result = g.evaluate("import pickle\npickle.loads(b'')")
    assert result.fail_mode == "silent"
    assert result.decision == "accept"


# ══════════════════════════════════════════════════════════════
#  13. Audit log behaviour
# ══════════════════════════════════════════════════════════════


def test_audit_written_for_warn_log(audit_tmp: Path) -> None:
    g = DeserializeGuard(fail_mode_override="warn+log", min_severity="low")
    g.evaluate("import pickle\npickle.loads(b'')")
    log = audit_tmp / "deserialize_guard.jsonl"
    assert log.exists()
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert record["guard"] == "deserialize_guard"
    assert record["decision"] == "warn"


def test_audit_written_for_hard_deny(audit_tmp: Path) -> None:
    g = DeserializeGuard(fail_mode_override="hard_deny", min_severity="low")
    g.evaluate("import pickle\npickle.loads(b'')")
    log = audit_tmp / "deserialize_guard.jsonl"
    assert log.exists()


def test_audit_skipped_for_silent_and_warn(audit_tmp: Path) -> None:
    DeserializeGuard(
        fail_mode_override="silent", min_severity="low",
    ).evaluate("import pickle\npickle.loads(b'')")
    DeserializeGuard(
        fail_mode_override="warn", min_severity="low",
    ).evaluate("import pickle\npickle.loads(b'')")
    log = audit_tmp / "deserialize_guard.jsonl"
    assert not log.exists()


def test_audit_entry_contains_findings(audit_tmp: Path) -> None:
    g = DeserializeGuard(fail_mode_override="warn+log", min_severity="low")
    g.evaluate("import pickle\npickle.loads(b'')")
    log = audit_tmp / "deserialize_guard.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert any(f["type"] == "pickle.loads" for f in rec["findings"])


# ══════════════════════════════════════════════════════════════
#  14. ZIQ outcome bus emit
# ══════════════════════════════════════════════════════════════


def test_ziq_emit_disabled_via_env(audit_tmp: Path) -> None:
    """``CONCINNO_ZIQ_BUS_DISABLED=1`` (set by audit_tmp) silences emit."""
    g = DeserializeGuard(fail_mode_override="warn+log", min_severity="low")
    # Should not raise even with no subscriber.
    g.evaluate("import pickle\npickle.loads(b'')")


def test_ziq_emit_subscriber_invoked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the bus is enabled, a subscriber sees the outcome."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus
    ZIQOutcomeBus._reset_for_testing()
    bus = ZIQOutcomeBus.get_bus()
    received: list[Outcome] = []
    bus.subscribe("security.deserialize_guard", received.append)

    g = DeserializeGuard(fail_mode_override="warn+log", min_severity="low")
    g.evaluate("import pickle\npickle.loads(b'')")
    assert len(received) == 1
    assert received[0].metadata["decision"] == "warn"
    ZIQOutcomeBus._reset_for_testing()


# ══════════════════════════════════════════════════════════════
#  15. Input format coverage
# ══════════════════════════════════════════════════════════════


def test_str_input(audit_tmp: Path) -> None:
    findings = _scan("import pickle\npickle.loads(b'')")
    assert findings


def test_bytes_input(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low")
    findings = g.scan(b"import pickle\npickle.loads(b'')")
    assert any(f.type == "pickle.loads" for f in findings)


def test_path_input(audit_tmp: Path, tmp_path: Path) -> None:
    src_path = tmp_path / "src.py"
    src_path.write_text("import pickle\npickle.loads(b'')\n", encoding="utf-8")
    g = DeserializeGuard(min_severity="low")
    findings = g.scan(src_path)
    assert any(f.type == "pickle.loads" for f in findings)


def test_ast_module_input(audit_tmp: Path) -> None:
    tree = ast.parse("import pickle\npickle.loads(b'')")
    g = DeserializeGuard(min_severity="low")
    findings = g.scan(tree)
    assert any(f.type == "pickle.loads" for f in findings)


def test_dict_input_yields_malformed(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low")
    findings = g.scan({"k": "v"})
    assert any(f.type == "malformed_payload" for f in findings)


def test_unknown_type_yields_malformed(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low")
    findings = g.scan(12345)  # type: ignore[arg-type]
    assert any(f.type == "malformed_payload" for f in findings)


def test_path_unreadable_yields_malformed(
    audit_tmp: Path, tmp_path: Path,
) -> None:
    g = DeserializeGuard(min_severity="low")
    findings = g.scan(tmp_path / "does_not_exist.py")
    assert any(f.type == "malformed_payload" for f in findings)


# ══════════════════════════════════════════════════════════════
#  16. AST parse-error handling
# ══════════════════════════════════════════════════════════════


def test_ast_parse_error_yields_finding(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low")
    findings = g.scan("def broken(:\n  pass")
    assert any(f.type == "parse_error" for f in findings)


def test_ast_parse_error_does_not_crash_evaluate(audit_tmp: Path) -> None:
    g = DeserializeGuard(fail_mode_override="warn", min_severity="low")
    result = g.evaluate("def broken(:\n  pass")
    # parse_error is severity=low so default min_severity=low keeps it.
    assert result.decision == "warn"


def test_empty_source_no_findings(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low")
    assert g.scan("") == []


def test_comment_only_source_no_findings(audit_tmp: Path) -> None:
    g = DeserializeGuard(min_severity="low")
    assert g.scan("# just a comment\n# pickle.loads here in text\n") == []


# ══════════════════════════════════════════════════════════════
#  17. Span / location reporting
# ══════════════════════════════════════════════════════════════


def test_finding_span_within_source(audit_tmp: Path) -> None:
    src = "import pickle\npickle.loads(b'')"
    findings = _scan(src)
    finds = [f for f in findings if f.type == "pickle.loads"]
    assert finds
    start, end = finds[0].span
    assert 0 <= start < end <= len(src)
    # The slice must contain the call expression.
    assert "pickle.loads" in src[start:end]


def test_finding_span_for_ast_module_input_is_minus_one(
    audit_tmp: Path,
) -> None:
    """Pre-parsed ``ast.Module`` lacks source → spans are (-1, -1)."""
    tree = ast.parse("import pickle\npickle.loads(b'')")
    g = DeserializeGuard(min_severity="low")
    finds = g.scan(tree)
    pickle_finds = [f for f in finds if f.type == "pickle.loads"]
    assert pickle_finds[0].span == (-1, -1)


# ══════════════════════════════════════════════════════════════
#  18. PATTERN_SEVERITY public mapping
# ══════════════════════════════════════════════════════════════


def test_pattern_severity_table_complete() -> None:
    """All advertised detectors must have an entry."""
    expected = {
        "pickle.load", "pickle.loads",
        "yaml.load", "yaml.unsafe_load",
        "dill.load", "dill.loads",
        "marshal.load", "marshal.loads",
        "eval", "exec", "eval.literal", "exec.literal",
        "__reduce__", "subprocess.Popen.shell",
    }
    assert expected.issubset(set(dg_mod.PATTERN_SEVERITY))


def test_safe_yaml_loaders_includes_safeloader() -> None:
    assert "SafeLoader" in dg_mod.SAFE_YAML_LOADERS
    assert "CSafeLoader" in dg_mod.SAFE_YAML_LOADERS


# ══════════════════════════════════════════════════════════════
#  19. FEATURE_META registration
# ══════════════════════════════════════════════════════════════


def test_feature_meta_registered() -> None:
    from concinno.feature_config import FEATURE_META

    assert "deserialize_guard" in FEATURE_META
    meta = FEATURE_META["deserialize_guard"]
    assert meta["category"] == "security"
    assert meta["enabled"] is True
    assert meta["ziq_autotunable"] is True
    assert meta["cosmetic"] is False
    for param in (
        "allow_pickle_with_protocol",
        "yaml_safe_loader_only",
        "flag_reduce_override",
        "min_severity",
    ):
        assert param in meta["params"]


def test_feature_meta_min_severity_options() -> None:
    from concinno.feature_config import FEATURE_META

    options = FEATURE_META["deserialize_guard"]["params"]["min_severity"]["options"]
    assert set(options) == {"low", "medium", "high", "critical"}


# ══════════════════════════════════════════════════════════════
#  20. Multiple findings + ordering
# ══════════════════════════════════════════════════════════════


def test_multiple_findings_in_single_source(audit_tmp: Path) -> None:
    src = (
        "import pickle\n"
        "import yaml\n"
        "import marshal\n"
        "pickle.loads(a)\n"
        "yaml.load(b)\n"
        "marshal.loads(c)\n"
        "eval(d)\n"
    )
    findings = _scan(src)
    types = _types(findings)
    assert {
        "pickle.loads", "yaml.load", "marshal.loads", "eval",
    }.issubset(types)


def test_findings_are_in_source_order(audit_tmp: Path) -> None:
    src = (
        "eval(a)\n"
        "import pickle\n"
        "pickle.loads(b)\n"
    )
    findings = _scan(src)
    # eval at line 1, pickle at line 3 — eval span starts before pickle's.
    eval_find = next(f for f in findings if f.type == "eval")
    pickle_find = next(f for f in findings if f.type == "pickle.loads")
    assert eval_find.span[0] < pickle_find.span[0]


def test_min_severity_high_drops_low_and_medium(audit_tmp: Path) -> None:
    src = (
        "class X:\n    def __reduce__(self): return ()\n"
        "import subprocess\nsubprocess.Popen(c, shell=True)\n"
        "import pickle\npickle.loads(b'')\n"
    )
    g = DeserializeGuard(min_severity="high")
    findings = g.scan(src)
    types = _types(findings)
    assert "__reduce__" not in types  # low → dropped
    assert "subprocess.Popen.shell" not in types  # medium → dropped
    assert "pickle.loads" in types  # critical → kept


# ══════════════════════════════════════════════════════════════
#  21. Regression — does not crash on common idioms
# ══════════════════════════════════════════════════════════════


_REGRESSION_SOURCES = [
    "x = 1",
    "def f(): pass",
    "class A: pass",
    "import os\nos.path.join('a', 'b')",
    "import json\nj = json.loads('{}')",
    "[i**2 for i in range(10)]",
    "with open('x') as f: data = f.read()",
    "lambda x: x + 1",
    "async def coro(): await foo()",
    "try: pass\nexcept Exception: pass",
    "@decorator\ndef f(): pass",
    "x: int = 5",
    "match x:\n    case 1: pass\n    case _: pass\n",
    "f'{value!r}'",
]


@pytest.mark.parametrize("source", _REGRESSION_SOURCES)
def test_regression_idiom_does_not_crash(audit_tmp: Path, source: str) -> None:
    g = DeserializeGuard(min_severity="low")
    # Must not raise; findings list may be empty or contain neutral entries.
    g.scan(source)


def test_repeat_evaluate_resets_state(audit_tmp: Path, tmp_path: Path) -> None:
    """Calling ``evaluate`` twice must not bleed test-fixture state."""
    fixtures = tmp_path / "tests"
    fixtures.mkdir()
    fixture_path = fixtures / "fix.py"
    fixture_path.write_text(
        "import pickle\npickle.loads(b'')\n", encoding="utf-8",
    )
    plain_path = tmp_path / "plain.py"
    plain_path.write_text(
        "import pickle\npickle.loads(b'')\n", encoding="utf-8",
    )

    g = DeserializeGuard(min_severity="low")
    r1 = g.evaluate(fixture_path)
    r2 = g.evaluate(plain_path)
    sev1 = next(f.severity for f in r1.findings if f.type == "pickle.loads")
    sev2 = next(f.severity for f in r2.findings if f.type == "pickle.loads")
    assert sev1 == "high"  # downgraded
    assert sev2 == "critical"  # not downgraded
