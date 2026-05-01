"""Regression tests for D1: ``_feature_param`` returned metadata dict instead of scalar.

Bug discovered by ``concinno-skills-lyceum-adapter`` 0.1.0 (R2-3 sub-agent
2026-05-01) when wiring RedBlueGreen review through Lyceum substrate. The
defensive ``try/except`` workaround in the adapter is fixed at the source
helper instead.

Sister helper ``wiredo_subagent_verify_guard._feature_param`` already had the
correct unwrap; ``redblue_green_dispatch_guard._feature_param`` was missed
during the FEATURE_META 5.0.0 default-on resurrection migration.
"""

from __future__ import annotations

import pytest

from concinno.guards.redblue_green_dispatch_guard import _feature_param


def test_fatal_threshold_returns_int_default():
    """Default value (no FEATURE_META entry) must return scalar default."""
    threshold = _feature_param("fatal_threshold", 3)
    assert isinstance(threshold, int), (
        f"Expected int, got {type(threshold).__name__} = {threshold!r}"
    )
    assert threshold == 3, f"Expected default 3, got {threshold!r}"


def test_fatal_threshold_callsite_int_cast_no_typeerror():
    """The int() cast at line 559/755/757 must not raise TypeError.

    Before the fix, ``params.get("fatal_threshold")`` returned the metadata
    dict ``{type, default, min, max, recommended}`` and ``int(dict)`` raised.
    """
    threshold = _feature_param("fatal_threshold", 3)
    # The assertion would fail with TypeError before the fix
    coerced = int(threshold)
    assert coerced >= 1, "Threshold must be a positive integer"


def test_unknown_param_returns_supplied_default():
    """Unknown param names must fall back to the supplied default verbatim."""
    sentinel = object()
    result = _feature_param("definitely_does_not_exist", sentinel)
    assert result is sentinel, f"Expected sentinel, got {result!r}"


def test_metadata_dict_is_unwrapped_to_scalar():
    """If FEATURE_META stores the schema dict, helper must extract ``default``."""
    from unittest.mock import patch

    fake_meta = {
        "redblue_green_review": {
            "params": {
                "fatal_threshold": {
                    "type": "int",
                    "default": 5,
                    "min": 1,
                    "max": 10,
                    "recommended": 3,
                }
            }
        }
    }

    with patch.dict("concinno.feature_config.FEATURE_META", fake_meta, clear=False):
        threshold = _feature_param("fatal_threshold", 3)

    assert isinstance(threshold, int), (
        f"Expected unwrapped int, got {type(threshold).__name__} = {threshold!r}"
    )
    assert threshold == 5, f"Expected unwrapped default 5, got {threshold!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
