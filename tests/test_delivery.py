"""Tests for concinno.delivery — Enterprise Delivery Gate."""

import json  # noqa: I001
import os

import pytest

from concinno.delivery import (
    Criterion,
    CriterionType,
    DeliveryGate,
    DeliveryReport,
    DeliveryState,
    ExitCriteria,
    OrphanExport,
    _defended_check,
    _detect_language,
    _extract_exports,
    _has_backend_files,
    _has_frontend_files,
    _has_screenshot_evidence,
    _has_test_evidence,
    _is_barrel_file,
    _is_symbol_imported,
    _parse_comma_names,
    auto_delivery_gate,
    check_orphan_exports,
    load_state,
    on_stop_check,
    save_state,
    scan_orphans_batch,
    wiredo_check,
)

# Backward compat alias
wired_check = wiredo_check


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def gate(tmp_path):
    return DeliveryGate(audit_dir=str(tmp_path / "audit"))


@pytest.fixture
def simple_criteria(gate):
    return gate.define_done(
        task="Fix auth bug",
        primary=["login test passes", "session token valid"],
        safety=["no regression in user tests"],
        task_id="test_001",
    )


# ── D1: ExitCriteria Definition ───────────────────────────


class TestDefineDone:
    def test_creates_criteria_with_primary_and_safety(self, gate):
        ec = gate.define_done(
            task="Add feature",
            primary=["unit test passes"],
            safety=["build succeeds"],
        )
        assert ec.task == "Add feature"
        assert len(ec.primary_criteria) == 1
        assert len(ec.safety_criteria) == 1
        assert len(ec.criteria) == 2

    def test_primary_only(self, gate):
        ec = gate.define_done(task="Quick fix", primary=["test passes"])
        assert len(ec.primary_criteria) == 1
        assert len(ec.safety_criteria) == 0

    def test_safety_only(self, gate):
        ec = gate.define_done(task="Refactor", safety=["no regression"])
        assert len(ec.primary_criteria) == 0
        assert len(ec.safety_criteria) == 1

    def test_empty_criteria_raises(self, gate):
        with pytest.raises(ValueError, match="At least one criterion"):
            gate.define_done(task="Empty")

    def test_empty_lists_raises(self, gate):
        with pytest.raises(ValueError, match="At least one criterion"):
            gate.define_done(task="Empty", primary=[], safety=[])

    def test_auto_task_id(self, gate):
        ec = gate.define_done(task="Auto ID", primary=["test"])
        assert ec.task_id.startswith("task_")

    def test_custom_task_id(self, gate):
        ec = gate.define_done(
            task="Custom", primary=["test"], task_id="my_id"
        )
        assert ec.task_id == "my_id"

    def test_registers_in_active(self, gate):
        gate.define_done(
            task="Track me", primary=["x"], task_id="track"
        )
        assert "track" in gate.active_tasks()

    def test_criteria_initially_unverified(self, gate):
        ec = gate.define_done(task="Init", primary=["a", "b"])
        for c in ec.criteria:
            assert c.passed is None
            assert c.evidence == ""

    def test_is_empty_property(self):
        ec = ExitCriteria(task="empty")
        assert ec.is_empty
        ec2 = ExitCriteria(
            task="not empty",
            criteria=[Criterion("x", CriterionType.PRIMARY)],
        )
        assert not ec2.is_empty

    def test_to_dict(self, gate):
        ec = gate.define_done(
            task="Dict test", primary=["p1"], safety=["s1"],
            task_id="dict_test",
        )
        d = ec.to_dict()
        assert d["task"] == "Dict test"
        assert d["task_id"] == "dict_test"
        assert len(d["criteria"]) == 2
        assert d["criteria"][0]["type"] == "primary"
        assert d["criteria"][1]["type"] == "safety"


# ── D2 + D3: Verify (Mechanical + Dual) ──────────────────


class TestVerify:
    def test_all_pass_bool(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        assert result.all_passed
        assert result.all_primary_passed
        assert result.all_safety_passed
        assert result.passed_count == 3

    def test_partial_pass(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": False,
            "no regression in user tests": True,
        })
        assert not result.all_passed
        assert not result.all_primary_passed
        assert result.all_safety_passed
        assert result.passed_count == 2

    def test_exit_code_zero_is_pass(self, gate):
        ec = gate.define_done(task="Exit code", primary=["tsc"])
        result = gate.verify(ec, evidence={"tsc": 0})
        assert result.all_passed
        assert ec.criteria[0].evidence == "exit code 0"

    def test_exit_code_nonzero_is_fail(self, gate):
        ec = gate.define_done(task="Exit code fail", primary=["tsc"])
        result = gate.verify(ec, evidence={"tsc": 1})
        assert not result.all_passed
        assert ec.criteria[0].evidence == "exit code 1"

    def test_string_evidence(self, gate):
        ec = gate.define_done(task="String", primary=["report"])
        result = gate.verify(ec, evidence={"report": "all clear"})
        assert result.all_passed
        assert ec.criteria[0].evidence == "all clear"

    def test_empty_string_is_fail(self, gate):
        ec = gate.define_done(task="Empty str", primary=["report"])
        result = gate.verify(ec, evidence={"report": ""})
        assert not result.all_passed

    def test_none_evidence_is_fail(self, gate):
        ec = gate.define_done(task="None", primary=["check"])
        result = gate.verify(ec, evidence={"check": None})
        assert not result.all_passed

    def test_missing_key_not_evaluated(self, gate):
        ec = gate.define_done(task="Missing", primary=["a", "b"])
        result = gate.verify(ec, evidence={"a": True})
        assert result.passed_count == 1
        assert ec.criteria[1].passed is None
        assert ec.criteria[1].evidence == "not evaluated"

    def test_no_evidence_dict(self, gate):
        ec = gate.define_done(task="No evidence", primary=["x"])
        result = gate.verify(ec)
        assert not result.all_passed

    def test_safety_fail_primary_pass(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": False,
        })
        assert result.all_primary_passed
        assert not result.all_safety_passed
        assert not result.all_passed

    def test_failed_criteria_list(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": False,
            "no regression in user tests": False,
        })
        failed = result.failed_criteria
        assert len(failed) == 2
        descs = {c.description for c in failed}
        assert "session token valid" in descs
        assert "no regression in user tests" in descs

    def test_to_dict(self, gate):
        ec = gate.define_done(task="Dict", primary=["a"], task_id="d1")
        result = gate.verify(ec, evidence={"a": True})
        d = result.to_dict()
        assert d["all_passed"] is True
        assert d["passed_count"] == 1
        assert d["total_count"] == 1

    def test_truthy_fallback(self, gate):
        ec = gate.define_done(task="Truthy", primary=["check"])
        result = gate.verify(ec, evidence={"check": [1, 2, 3]})
        assert result.all_passed


# ── D4: Three-State Report ────────────────────────────────


class TestReport:
    def test_pass_state(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        report = gate.report(simple_criteria, result)
        assert report.state == DeliveryState.PASS
        assert "3/3" in report.summary

    def test_partial_state(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": False,
            "no regression in user tests": True,
        })
        report = gate.report(simple_criteria, result)
        assert report.state == DeliveryState.PARTIAL
        assert "2/3" in report.summary

    def test_fail_state(self, gate):
        ec = gate.define_done(task="All fail", primary=["a", "b"])
        result = gate.verify(ec, evidence={"a": False, "b": False})
        report = gate.report(ec, result)
        assert report.state == DeliveryState.FAIL
        assert "0/2" in report.summary

    def test_blockers_and_attempts(self, gate):
        ec = gate.define_done(task="Blocked", primary=["a"])
        result = gate.verify(ec, evidence={"a": False})
        report = gate.report(
            ec, result,
            blockers=["API key missing"],
            attempts=["tried env var", "tried config file"],
        )
        assert report.blockers == ["API key missing"]
        assert len(report.attempts) == 2

    def test_emoji(self):
        assert DeliveryReport(
            DeliveryState.PASS, "t", "s"
        ).emoji == "✅"
        assert DeliveryReport(
            DeliveryState.PARTIAL, "t", "s"
        ).emoji == "⏸"
        assert DeliveryReport(
            DeliveryState.FAIL, "t", "s"
        ).emoji == "❌"

    def test_to_dict_no_blockers(self, gate):
        ec = gate.define_done(task="Clean", primary=["a"])
        result = gate.verify(ec, evidence={"a": True})
        report = gate.report(ec, result)
        d = report.to_dict()
        assert "blockers" not in d
        assert "attempts" not in d

    def test_to_dict_with_blockers(self, gate):
        ec = gate.define_done(task="Blocked", primary=["a"])
        result = gate.verify(ec, evidence={"a": False})
        report = gate.report(
            ec, result, blockers=["x"], attempts=["y"]
        )
        d = report.to_dict()
        assert d["blockers"] == ["x"]
        assert d["attempts"] == ["y"]

    def test_format_text_pass(self, gate):
        ec = gate.define_done(task="Format", primary=["test"])
        result = gate.verify(ec, evidence={"test": True})
        report = gate.report(ec, result)
        text = report.format_text()
        assert "✅" in text
        assert "Format" in text

    def test_format_text_fail_with_blockers(self, gate):
        ec = gate.define_done(task="Format fail", primary=["test"])
        result = gate.verify(ec, evidence={"test": False})
        report = gate.report(
            ec, result,
            blockers=["timeout"],
            attempts=["retry 3x"],
        )
        text = report.format_text()
        assert "❌" in text
        assert "Blockers:" in text
        assert "timeout" in text
        assert "Attempted:" in text


# ── D5: Karpathy Loop ────────────────────────────────────


class TestKarpathyLoop:
    def test_no_retry_when_passed(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        assert not gate.should_retry(result)

    def test_retry_when_failed(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": False,
            "no regression in user tests": True,
        })
        assert gate.should_retry(result, current_iteration=0)

    def test_no_retry_at_max_iterations(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": False,
            "session token valid": False,
            "no regression in user tests": True,
        })
        assert not gate.should_retry(result, max_iterations=3, current_iteration=3)

    def test_no_retry_when_all_unevaluated(self, gate):
        ec = gate.define_done(task="No eval", primary=["a", "b"])
        result = gate.verify(ec)  # no evidence
        assert not gate.should_retry(result)

    def test_rollback_on_safety_fail(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": False,
        })
        assert gate.rollback_decision(result)

    def test_no_rollback_on_primary_fail_only(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": False,
            "session token valid": False,
            "no regression in user tests": True,
        })
        assert not gate.rollback_decision(result)

    def test_no_rollback_all_pass(self, gate, simple_criteria):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        assert not gate.rollback_decision(result)


# ── D6: Audit Log ─────────────────────────────────────────


class TestAuditLog:
    def test_writes_jsonl(self, gate, simple_criteria, tmp_path):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        report = gate.report(simple_criteria, result)
        path = gate.audit_log(simple_criteria, result, report)
        assert path.endswith("delivery_audit.jsonl")
        assert os.path.isfile(path)

        with open(path, "r", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert entry["task_id"] == "test_001"
        assert entry["state"] == "pass"
        assert entry["passed"] == 3

    def test_appends_multiple(self, gate, tmp_path):
        ec1 = gate.define_done(
            task="Task 1", primary=["a"], task_id="t1"
        )
        r1 = gate.verify(ec1, evidence={"a": True})
        rp1 = gate.report(ec1, r1)
        gate.audit_log(ec1, r1, rp1)

        ec2 = gate.define_done(
            task="Task 2", primary=["b"], task_id="t2"
        )
        r2 = gate.verify(ec2, evidence={"b": False})
        rp2 = gate.report(ec2, r2)
        path = gate.audit_log(ec2, r2, rp2)

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2

    def test_extra_metadata(self, gate, simple_criteria, tmp_path):
        result = gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        report = gate.report(simple_criteria, result)
        path = gate.audit_log(
            simple_criteria, result, report,
            extra={"session_id": "sess_123", "agent": "opus"},
        )
        with open(path, "r", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert entry["extra"]["session_id"] == "sess_123"

    def test_no_audit_dir_no_workspace(self, tmp_path):
        gate = DeliveryGate()  # no audit_dir
        ec = gate.define_done(task="No dir", primary=["a"])
        result = gate.verify(ec, evidence={"a": True})
        report = gate.report(ec, result)
        # With no CLAUDE_PROJECT_DIR, returns empty
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        try:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            path = gate.audit_log(ec, result, report)
            assert path == ""
        finally:
            if old is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_audit_with_env_workspace(self, tmp_path):
        gate = DeliveryGate()  # no explicit audit_dir
        ec = gate.define_done(task="Env dir", primary=["a"])
        result = gate.verify(ec, evidence={"a": True})
        report = gate.report(ec, result)
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        try:
            os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_path)
            path = gate.audit_log(ec, result, report)
            assert path != ""
            assert os.path.isfile(path)
        finally:
            if old is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = old
            else:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)


# ── D7: Gate Check ────────────────────────────────────────


class TestGateCheck:
    def test_no_task_id_allows(self, gate):
        assert gate.gate_check() is None

    def test_unknown_task_allows(self, gate):
        assert gate.gate_check("nonexistent") is None

    def test_unverified_blocks(self, gate, simple_criteria):
        reason = gate.gate_check("test_001")
        assert reason is not None
        assert "no verification" in reason.lower()

    def test_failed_blocks(self, gate, simple_criteria):
        gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": False,
            "no regression in user tests": True,
        })
        reason = gate.gate_check("test_001")
        assert reason is not None
        assert "session token valid" in reason

    def test_all_passed_allows(self, gate, simple_criteria):
        gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        assert gate.gate_check("test_001") is None


# ── Introspection ─────────────────────────────────────────


class TestIntrospection:
    def test_active_tasks(self, gate, simple_criteria):
        tasks = gate.active_tasks()
        assert "test_001" in tasks
        assert tasks["test_001"]["task"] == "Fix auth bug"
        assert tasks["test_001"]["criteria_count"] == 3
        assert tasks["test_001"]["verified"] is False

    def test_active_tasks_after_verify(self, gate, simple_criteria):
        gate.verify(simple_criteria, evidence={
            "login test passes": True,
            "session token valid": True,
            "no regression in user tests": True,
        })
        tasks = gate.active_tasks()
        assert tasks["test_001"]["verified"] is True
        assert tasks["test_001"]["all_passed"] is True

    def test_clear_task(self, gate, simple_criteria):
        assert gate.clear_task("test_001") is True
        assert "test_001" not in gate.active_tasks()

    def test_clear_nonexistent(self, gate):
        assert gate.clear_task("nope") is False


# ── Edge Cases ────────────────────────────────────────────


class TestEdgeCases:
    def test_criterion_type_values(self):
        assert CriterionType.PRIMARY.value == "primary"
        assert CriterionType.SAFETY.value == "safety"

    def test_delivery_state_values(self):
        assert DeliveryState.PASS.value == "pass"
        assert DeliveryState.PARTIAL.value == "partial"
        assert DeliveryState.FAIL.value == "fail"

    def test_criterion_to_dict(self):
        c = Criterion("test", CriterionType.PRIMARY, True, "ok")
        d = c.to_dict()
        assert d["description"] == "test"
        assert d["type"] == "primary"
        assert d["passed"] is True
        assert d["evidence"] == "ok"

    def test_multiple_tasks_independent(self, gate):
        ec1 = gate.define_done(
            task="Task A", primary=["a"], task_id="ta"
        )
        ec2 = gate.define_done(
            task="Task B", primary=["b"], task_id="tb"
        )
        gate.verify(ec1, evidence={"a": True})
        gate.verify(ec2, evidence={"b": False})
        assert gate.gate_check("ta") is None
        assert gate.gate_check("tb") is not None

    def test_re_verify_overwrites(self, gate):
        ec = gate.define_done(
            task="Re-verify", primary=["a"], task_id="rv"
        )
        r1 = gate.verify(ec, evidence={"a": False})
        assert not r1.all_passed
        r2 = gate.verify(ec, evidence={"a": True})
        assert r2.all_passed
        assert gate.gate_check("rv") is None


# ── D8: Orphan Export Detection ──────────────────────────


class TestDetectLanguage:
    def test_typescript_extensions(self):
        assert _detect_language("src/foo.ts") == "typescript"
        assert _detect_language("src/foo.tsx") == "typescript"
        assert _detect_language("src/foo.js") == "typescript"
        assert _detect_language("src/foo.jsx") == "typescript"
        assert _detect_language("src/foo.mts") == "typescript"
        assert _detect_language("src/foo.mjs") == "typescript"

    def test_python(self):
        assert _detect_language("src/foo.py") == "python"

    def test_unknown(self):
        assert _detect_language("src/foo.rs") == ""
        assert _detect_language("README.md") == ""
        assert _detect_language("data.json") == ""


class TestIsBarrelFile:
    def test_barrel_files(self):
        assert _is_barrel_file("index.ts")
        assert _is_barrel_file("index.js")
        assert _is_barrel_file("index.mts")
        assert _is_barrel_file("index.mjs")
        assert _is_barrel_file("__init__.py")
        assert _is_barrel_file("mod.rs")

    def test_non_barrel(self):
        assert not _is_barrel_file("router.ts")
        assert not _is_barrel_file("main.py")
        assert not _is_barrel_file("index.tsx")


class TestExtractExports:
    def test_ts_function(self):
        code = "export function routeArchitecture() {}"
        assert _extract_exports(code, "typescript") == ["routeArchitecture"]

    def test_ts_class_const_type(self):
        code = (
            "export class MyService {}\n"
            "export const CONFIG = 1\n"
            "export type Opts = {}\n"
            "export interface IFoo {}\n"
            "export enum Status { A, B }\n"
        )
        result = _extract_exports(code, "typescript")
        assert set(result) == {"MyService", "CONFIG", "Opts", "IFoo", "Status"}

    def test_ts_named_export(self):
        code = "export { alpha, beta as gamma }"
        result = _extract_exports(code, "typescript")
        assert "alpha" in result
        # "as gamma" → extracts the original name (before "as")
        assert "beta" in result

    def test_ts_named_export_whitespace(self):
        code = "export {\n  foo ,\n  bar\n}"
        result = _extract_exports(code, "typescript")
        assert set(result) == {"foo", "bar"}

    def test_python_all(self):
        code = "__all__ = ['one', 'two', 'three']"
        result = _extract_exports(code, "python")
        assert result == ["one", "two", "three"]

    def test_python_all_double_quotes(self):
        code = '__all__ = ["alpha", "beta"]'
        result = _extract_exports(code, "python")
        assert result == ["alpha", "beta"]

    def test_python_defs_no_all(self):
        code = (
            "def public_func():\n    pass\n\n"
            "def _private():\n    pass\n\n"
            "class MyClass:\n    pass"
        )
        result = _extract_exports(code, "python")
        assert "public_func" in result
        assert "MyClass" in result
        assert "_private" not in result

    def test_python_all_overrides_defs(self):
        code = (
            "__all__ = ['only_this']\n"
            "def only_this(): pass\n"
            "def also_public(): pass\n"
        )
        result = _extract_exports(code, "python")
        assert result == ["only_this"]

    def test_empty_content(self):
        assert _extract_exports("", "typescript") == []
        assert _extract_exports("", "python") == []

    def test_unknown_language(self):
        assert _extract_exports("export function foo() {}", "") == []


class TestIsSymbolImported:
    def test_found_in_another_file(self, tmp_path):
        # source exports "myFunc"
        src = tmp_path / "src" / "mod.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function myFunc() {}", encoding="utf-8")
        # consumer imports it
        consumer = tmp_path / "src" / "app.ts"
        consumer.write_text('import { myFunc } from "./mod"', encoding="utf-8")
        assert _is_symbol_imported("myFunc", str(src), str(tmp_path), "typescript")

    def test_not_found(self, tmp_path):
        src = tmp_path / "src" / "mod.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function orphan() {}", encoding="utf-8")
        other = tmp_path / "src" / "app.ts"
        other.write_text("console.log('nothing')", encoding="utf-8")
        assert not _is_symbol_imported("orphan", str(src), str(tmp_path), "typescript")

    @pytest.mark.xfail(reason="barrel exclusion not yet implemented")
    def test_barrel_excluded_by_default(self, tmp_path):
        src = tmp_path / "src" / "mod.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function myFunc() {}", encoding="utf-8")
        barrel = tmp_path / "src" / "index.ts"
        barrel.write_text('export { myFunc } from "./mod"', encoding="utf-8")
        # barrel is the only "importer" → should NOT count
        assert not _is_symbol_imported("myFunc", str(src), str(tmp_path), "typescript")

    @pytest.mark.xfail(reason="barrel exclusion not yet implemented")
    def test_barrel_counted_when_disabled(self, tmp_path):
        src = tmp_path / "src" / "mod.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function myFunc() {}", encoding="utf-8")
        barrel = tmp_path / "src" / "index.ts"
        barrel.write_text('export { myFunc } from "./mod"', encoding="utf-8")
        assert _is_symbol_imported(
            "myFunc", str(src), str(tmp_path), "typescript", exclude_barrels=False,
        )

    def test_skips_node_modules(self, tmp_path):
        src = tmp_path / "src" / "mod.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function myFunc() {}", encoding="utf-8")
        nm = tmp_path / "node_modules" / "pkg" / "use.ts"
        nm.parent.mkdir(parents=True)
        nm.write_text("import { myFunc } from 'mod'", encoding="utf-8")
        assert not _is_symbol_imported("myFunc", str(src), str(tmp_path), "typescript")

    def test_python_import(self, tmp_path):
        src = tmp_path / "pkg" / "helper.py"
        src.parent.mkdir(parents=True)
        src.write_text("def compute(): pass", encoding="utf-8")
        consumer = tmp_path / "pkg" / "main.py"
        consumer.write_text("from .helper import compute", encoding="utf-8")
        assert _is_symbol_imported("compute", str(src), str(tmp_path), "python")


class TestCheckOrphanExports:
    def test_ts_orphan_detected(self, tmp_path):
        src = tmp_path / "src" / "island.ts"
        src.parent.mkdir(parents=True)
        src.write_text(
            "export function orphanA() {}\nexport const orphanB = 1\n",
            encoding="utf-8",
        )
        result = check_orphan_exports(str(src), str(tmp_path))
        names = {o.symbol for o in result}
        assert names == {"orphanA", "orphanB"}
        assert all(o.language == "typescript" for o in result)

    def test_ts_no_orphan_when_imported(self, tmp_path):
        src = tmp_path / "src" / "util.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function helper() {}", encoding="utf-8")
        consumer = tmp_path / "src" / "app.ts"
        consumer.write_text('import { helper } from "./util"', encoding="utf-8")
        result = check_orphan_exports(str(src), str(tmp_path))
        assert result == []

    def test_python_orphan_detected(self, tmp_path):
        src = tmp_path / "pkg" / "orphan.py"
        src.parent.mkdir(parents=True)
        src.write_text(
            "__all__ = ['gone', 'missing']\ndef gone(): pass\ndef missing(): pass\n",
            encoding="utf-8",
        )
        result = check_orphan_exports(str(src), str(tmp_path))
        names = {o.symbol for o in result}
        assert names == {"gone", "missing"}

    def test_unknown_language_returns_empty(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text("{}", encoding="utf-8")
        assert check_orphan_exports(str(f), str(tmp_path)) == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        assert check_orphan_exports(str(tmp_path / "nope.ts"), str(tmp_path)) == []

    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "empty.ts"
        f.write_text("", encoding="utf-8")
        assert check_orphan_exports(str(f), str(tmp_path)) == []

    @pytest.mark.xfail(reason="barrel exclusion not yet implemented")
    def test_barrel_reexport_not_counted(self, tmp_path):
        """Barrel re-exports don't save a symbol from being orphan."""
        src = tmp_path / "src" / "deep.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function deepFn() {}", encoding="utf-8")
        barrel = tmp_path / "src" / "index.ts"
        barrel.write_text('export { deepFn } from "./deep"', encoding="utf-8")
        result = check_orphan_exports(str(src), str(tmp_path))
        assert len(result) == 1
        assert result[0].symbol == "deepFn"

    def test_mixed_orphan_and_used(self, tmp_path):
        src = tmp_path / "src" / "mix.ts"
        src.parent.mkdir(parents=True)
        src.write_text(
            "export function used() {}\nexport function unused() {}\n",
            encoding="utf-8",
        )
        consumer = tmp_path / "src" / "app.ts"
        consumer.write_text("const x = used()", encoding="utf-8")
        result = check_orphan_exports(str(src), str(tmp_path))
        names = {o.symbol for o in result}
        assert "unused" in names
        assert "used" not in names


class TestOrphanExportDataclass:
    def test_str(self):
        o = OrphanExport(
            symbol="foo", file_path="src/bar.ts", language="typescript",
        )
        s = "orphan: foo in src/bar.ts (typescript)"
        assert str(o) == s

    def test_fields(self):
        o = OrphanExport(symbol="s", file_path="f.py", language="python")
        assert o.symbol == "s"
        assert o.file_path == "f.py"
        assert o.language == "python"


class TestScanOrphansBatch:
    def test_multiple_files(self, tmp_path):
        src1 = tmp_path / "src" / "a.ts"
        src1.parent.mkdir(parents=True)
        src1.write_text("export function aFn() {}", encoding="utf-8")
        src2 = tmp_path / "src" / "b.py"
        src2.write_text("def bFn(): pass", encoding="utf-8")
        result = scan_orphans_batch(
            [str(src1), str(src2)], str(tmp_path),
        )
        names = {o.symbol for o in result}
        assert "aFn" in names
        assert "bFn" in names

    def test_relative_paths(self, tmp_path):
        src = tmp_path / "mod.ts"
        src.write_text("export const X = 1", encoding="utf-8")
        result = scan_orphans_batch(["mod.ts"], str(tmp_path))
        assert len(result) == 1
        assert result[0].symbol == "X"

    def test_skips_unknown_language(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text("{}", encoding="utf-8")
        assert scan_orphans_batch([str(f)], str(tmp_path)) == []

    def test_empty_list(self, tmp_path):
        assert scan_orphans_batch([], str(tmp_path)) == []

    def test_mixed_orphan_and_used(self, tmp_path):
        src = tmp_path / "lib.ts"
        src.write_text(
            "export function used() {}\n"
            "export function orphan() {}\n",
            encoding="utf-8",
        )
        consumer = tmp_path / "app.ts"
        consumer.write_text("const x = used()", encoding="utf-8")
        result = scan_orphans_batch([str(src)], str(tmp_path))
        names = {o.symbol for o in result}
        assert "orphan" in names
        assert "used" not in names


# ── Persistence (save/load/on_stop_check) ─────────────────


class TestSaveLoadState:
    def test_roundtrip_empty(self, tmp_path):
        gate = DeliveryGate()
        save_state(gate, str(tmp_path))
        loaded = load_state(str(tmp_path))
        assert loaded.active_tasks() == {}

    def test_roundtrip_with_criteria(self, tmp_path):
        gate = DeliveryGate()
        gate.define_done("Fix bug", primary=["test pass"], task_id="t1")
        save_state(gate, str(tmp_path))
        loaded = load_state(str(tmp_path))
        tasks = loaded.active_tasks()
        assert "t1" in tasks
        assert tasks["t1"]["task"] == "Fix bug"
        assert tasks["t1"]["verified"] is False

    def test_roundtrip_with_verified(self, tmp_path):
        gate = DeliveryGate()
        criteria = gate.define_done(
            "Add auth", primary=["login works"], task_id="t2",
        )
        gate.verify(criteria, evidence={"login works": True})
        save_state(gate, str(tmp_path))
        loaded = load_state(str(tmp_path))
        tasks = loaded.active_tasks()
        assert tasks["t2"]["verified"] is True
        assert tasks["t2"]["all_passed"] is True

    def test_roundtrip_with_failed(self, tmp_path):
        gate = DeliveryGate()
        criteria = gate.define_done(
            "Deploy", primary=["build ok"], safety=["no regression"],
            task_id="t3",
        )
        gate.verify(criteria, evidence={"build ok": True})
        save_state(gate, str(tmp_path))
        loaded = load_state(str(tmp_path))
        tasks = loaded.active_tasks()
        assert tasks["t3"]["all_passed"] is False
        assert tasks["t3"]["passed_count"] == 1

    def test_load_missing_file(self, tmp_path):
        loaded = load_state(str(tmp_path / "nonexistent"))
        assert loaded.active_tasks() == {}

    def test_load_corrupt_json(self, tmp_path):
        state_path = tmp_path / "delivery_state.json"
        state_path.write_text("not json", encoding="utf-8")
        loaded = load_state(str(tmp_path))
        assert loaded.active_tasks() == {}


class TestOnStopCheck:
    def test_no_tasks(self, tmp_path):
        assert on_stop_check(str(tmp_path)) == ""

    def test_all_passed(self, tmp_path):
        gate = DeliveryGate()
        c = gate.define_done("OK task", primary=["p1"], task_id="ok")
        gate.verify(c, evidence={"p1": True})
        save_state(gate, str(tmp_path))
        report = on_stop_check(str(tmp_path))
        # All passed → report contains ✅ summary (not empty)
        assert "✅" in report

    def test_unverified_task(self, tmp_path):
        gate = DeliveryGate()
        gate.define_done("Pending", primary=["check"], task_id="pend")
        save_state(gate, str(tmp_path))
        report = on_stop_check(str(tmp_path))
        assert "Pending" in report
        assert "NEVER verified" in report or "unverified" in report.lower()

    def test_failed_task(self, tmp_path):
        gate = DeliveryGate()
        c = gate.define_done(
            "Broken", primary=["a"], safety=["b"], task_id="fail",
        )
        gate.verify(c, evidence={"a": True})  # b not passed
        save_state(gate, str(tmp_path))
        report = on_stop_check(str(tmp_path))
        assert "❌" in report
        assert "Broken" in report

    def test_mixed_tasks(self, tmp_path):
        gate = DeliveryGate()
        c1 = gate.define_done("Good", primary=["x"], task_id="g")
        gate.verify(c1, evidence={"x": True})
        gate.define_done("Bad", primary=["y"], task_id="b")
        save_state(gate, str(tmp_path))
        report = on_stop_check(str(tmp_path))
        assert "✅" in report
        assert "⚠" in report  # unverified task warning


# ── WIREDO-D: Defended & Verified ─────────────────────────


class TestParseCommaNames:
    def test_basic(self):
        assert _parse_comma_names("foo, bar, baz") == ["foo", "bar", "baz"]

    def test_as_alias(self):
        assert _parse_comma_names("foo as f, bar") == ["foo", "bar"]

    def test_strip_chars(self):
        assert _parse_comma_names("'foo', 'bar'", "'\"") == ["foo", "bar"]

    def test_empty_parts(self):
        assert _parse_comma_names("  ,  , ") == []

    def test_non_identifier(self):
        assert _parse_comma_names("123, valid") == ["valid"]


class TestHasFrontendFiles:
    def test_tsx(self):
        assert _has_frontend_files(["src/App.tsx"])

    def test_css(self):
        assert _has_frontend_files(["styles/main.css"])

    def test_py_only(self):
        assert not _has_frontend_files(["src/main.py"])

    def test_empty(self):
        assert not _has_frontend_files([])


class TestHasBackendFiles:
    def test_py(self):
        assert _has_backend_files(["src/api.py"])

    def test_excludes_test(self):
        assert not _has_backend_files(["tests/test_api.py"])

    def test_excludes_spec(self):
        assert not _has_backend_files(["src/api.spec.ts"])

    def test_tsx_not_backend(self):
        assert not _has_backend_files(["src/App.tsx"])


class TestHasScreenshotEvidence:
    def test_no_state(self, tmp_path):
        assert not _has_screenshot_evidence(str(tmp_path), "sid1")

    def test_verified(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("ui_verify", "sid1", {"verified": True})
        assert _has_screenshot_evidence(str(tmp_path), "sid1")

    def test_verify_fails(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("ui_verify", "sid1", {"verify_fails": 1})
        assert _has_screenshot_evidence(str(tmp_path), "sid1")


class TestHasTestEvidence:
    def test_no_state(self, tmp_path):
        assert not _has_test_evidence(str(tmp_path), "sid1")

    def test_pytest_found(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("sentinel", "sid1", {
            "calls": [{"tool": "Bash", "bash_pfx": "pytest tests/"}],
        })
        assert _has_test_evidence(str(tmp_path), "sid1")

    def test_vitest_found(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("sentinel", "sid1", {
            "calls": [{"tool": "Bash", "bash_pfx": "npx vitest run"}],
        })
        assert _has_test_evidence(str(tmp_path), "sid1")

    def test_non_test_bash(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("sentinel", "sid1", {
            "calls": [{"tool": "Bash", "bash_pfx": "git status"}],
        })
        assert not _has_test_evidence(str(tmp_path), "sid1")


class TestDefendedCheck:
    def test_no_files(self, tmp_path):
        assert _defended_check([], str(tmp_path), "sid1") == []

    def test_frontend_no_screenshot(self, tmp_path):
        lines = _defended_check(["src/App.tsx"], str(tmp_path), "sid1")
        assert any("D(frontend)" in ln for ln in lines)

    def test_frontend_with_screenshot(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("ui_verify", "sid1", {"verified": True})
        lines = _defended_check(["src/App.tsx"], str(tmp_path), "sid1")
        assert not any("D(frontend)" in ln for ln in lines)

    def test_backend_no_test(self, tmp_path):
        lines = _defended_check(["src/api.py"], str(tmp_path), "sid1")
        assert any("D(backend)" in ln for ln in lines)

    def test_backend_with_test(self, tmp_path):
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        store.write("sentinel", "sid1", {
            "calls": [{"tool": "Bash", "bash_pfx": "pytest"}],
        })
        lines = _defended_check(["src/api.py"], str(tmp_path), "sid1")
        assert not any("D(backend)" in ln for ln in lines)

    def test_both_missing(self, tmp_path):
        lines = _defended_check(
            ["src/App.tsx", "src/api.py"], str(tmp_path), "sid1",
        )
        assert any("D(frontend)" in ln for ln in lines)
        assert any("D(backend)" in ln for ln in lines)


class TestWiredCheckWithD:
    def test_includes_d_report(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        from concinno.core.state_store import StateStore
        store = StateStore(str(tmp_path / ".concinno_cache"))
        # Write sentinel with frontend file edited, no screenshot
        store.write("sentinel", "sid1", {
            "edited_files": [str(tmp_path / "App.tsx")],
        })
        # Create the file so _get_session_code_files finds it
        (tmp_path / "App.tsx").write_text("export const X = 1;")
        report = wired_check(
            str(tmp_path / ".concinno_cache"), "sid1",
        )
        assert "WIREDO-D" in report
        assert "D(frontend)" in report


# ── auto_delivery_gate (CCC self-use D1→D2→D4→D6) ─────────


class TestAutoDeliveryGate:
    def test_empty_when_no_files(self, tmp_path):
        result = auto_delivery_gate(str(tmp_path), "empty_session")
        assert result == ""

    def test_all_pass_backend_with_test(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cache_dir = str(tmp_path / ".concinno_cache")
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)

        # Create a wired backend file
        mod = tmp_path / "utils.py"
        mod.write_text("def helper(): pass\n")
        consumer = tmp_path / "main.py"
        consumer.write_text("from utils import helper\n")

        # Sentinel: file edited + test run
        store.write("sentinel", "sid1", {
            "edited_files": [str(mod)],
            "calls": [{"tool": "Bash", "bash_pfx": "pytest"}],
        })

        result = auto_delivery_gate(cache_dir, "sid1")
        assert "✅" in result
        assert "DeliveryGate" in result

    def test_fail_unwired_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cache_dir = str(tmp_path / ".concinno_cache")
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)

        orphan = tmp_path / "orphan_module.py"
        orphan.write_text("def lonely(): pass\n")

        store.write("sentinel", "sid1", {
            "edited_files": [str(orphan)],
            "calls": [{"tool": "Bash", "bash_pfx": "pytest"}],
        })

        result = auto_delivery_gate(cache_dir, "sid1")
        assert "❌" in result
        assert "wired" in result.lower()

    def test_fail_frontend_no_screenshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cache_dir = str(tmp_path / ".concinno_cache")
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)

        # Frontend file + wired
        comp = tmp_path / "Button.tsx"
        comp.write_text("export const Button = () => {};")
        consumer = tmp_path / "App.tsx"
        consumer.write_text("import { Button } from './Button';")

        store.write("sentinel", "sid1", {
            "edited_files": [str(comp)],
        })

        result = auto_delivery_gate(cache_dir, "sid1")
        assert "❌" in result
        assert "screenshot" in result.lower()

    def test_fail_backend_no_test(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cache_dir = str(tmp_path / ".concinno_cache")
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)

        mod = tmp_path / "api.py"
        mod.write_text("def endpoint(): pass\n")
        consumer = tmp_path / "router.py"
        consumer.write_text("from api import endpoint\n")

        store.write("sentinel", "sid1", {
            "edited_files": [str(mod)],
        })

        result = auto_delivery_gate(cache_dir, "sid1")
        assert "❌" in result
        assert "test" in result.lower()

    def test_audit_log_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cache_dir = str(tmp_path / ".concinno_cache")
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)

        mod = tmp_path / "svc.py"
        mod.write_text("def run(): pass\n")
        consumer = tmp_path / "main.py"
        consumer.write_text("from svc import run\n")
        store.write("sentinel", "sid1", {
            "edited_files": [str(mod)],
            "calls": [{"tool": "Bash", "bash_pfx": "pytest"}],
        })

        auto_delivery_gate(cache_dir, "sid1")

        audit_path = os.path.join(cache_dir, "delivery_audit", "delivery_audit.jsonl")
        assert os.path.isfile(audit_path)
        with open(audit_path) as f:
            entry = json.loads(f.readline())
        assert entry["state"] in ("pass", "partial", "fail")
        assert "code_files" in entry.get("extra", {})
