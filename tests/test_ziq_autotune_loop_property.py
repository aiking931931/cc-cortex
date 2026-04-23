"""Property-test the cascade never violates PresetModel schema.

narrower-scope v4 §1 property-test invariant: for any
(preset_name, feature_key) sampled from the discovery set, the resulting
``preset.json`` on disk must still parse via ``PresetsFile.model_validate``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from concinno.preset_cascade import (
    _user_preset_path,
    list_presets,
    load_presets_file,
    set_preset,
    set_preset_value,
)
from concinno.preset_model import PresetsFile


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    import concinno.core.config as _ccfg_mod
    from concinno.core.config import get_config

    hooks_tmp = tmp_path / "hooks"
    hooks_tmp.mkdir(parents=True, exist_ok=True)
    _ccfg_mod._instance = None  # type: ignore[attr-defined]
    get_config(hooks_dir=str(hooks_tmp))
    yield tmp_path
    _ccfg_mod._instance = None  # type: ignore[attr-defined]


# Sample from the actual packaged preset set + a stable list of
# autotunable feature keys per v4 design doc.
PRESET_NAMES = list_presets()
FEATURE_KEYS = [
    "token_gate.agent_threshold",
    "consecutive_fail_gate.max_fails",
    "sentinel_gate.max_repeats",
    "delivery_gate.max_iterations",
    "butterfly_guard.max_fix_attempts",
]
VALUES = st.one_of(
    st.integers(min_value=1, max_value=1_000_000),
    st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    st.booleans(),
)


@given(
    preset_name=st.sampled_from(PRESET_NAMES),
    key=st.sampled_from(FEATURE_KEYS),
    value=VALUES,
)
@settings(deadline=None, max_examples=25)
def test_cascade_never_violates_schema(
    preset_name: str, key: str, value: object
) -> None:
    """After set_preset_value, user preset.json still parses."""
    ok = set_preset_value(preset_name, key, value)
    assert ok
    user_path = _user_preset_path()
    assert user_path.is_file()
    raw = json.loads(user_path.read_text(encoding="utf-8"))
    # Must re-validate through the Pydantic model.
    parsed = PresetsFile.model_validate(raw)
    assert preset_name in parsed.presets
    assert parsed.presets[preset_name].summary[key] == value


@given(preset_name=st.sampled_from(PRESET_NAMES))
@settings(deadline=None, max_examples=10)
def test_set_preset_after_many_writes_still_loads(
    preset_name: str,
) -> None:
    """set_preset() is robust after arbitrary set_preset_value churn."""
    for k in FEATURE_KEYS[:3]:
        set_preset_value(preset_name, k, 42)
    # Now switching should not raise.
    report = set_preset(preset_name)
    assert report.preset_name == preset_name
    # The merged view still parses.
    merged = load_presets_file()
    assert preset_name in merged.presets
