"""Tests for sub-agent K Task 4 — A↔B compress_breakeven_tokens dedup.

Verifies the ZIQ autotune registry resolves
``field_read.compress_breakeven_tokens`` from
``feature_config.FEATURE_META`` (single source of truth) instead of
maintaining a hardcoded duplicate.
"""

from __future__ import annotations

from concinno.feature_config import FEATURE_META
from concinno.ziq_autotune_registry import describe


def test_compress_breakeven_in_registry() -> None:
    spec = describe("field_read.compress_breakeven_tokens")
    assert spec.target == "field_read.compress_breakeven_tokens"


def test_compress_breakeven_aligned_with_feature_meta() -> None:
    """Registry preset/vmin/vmax MUST match FEATURE_META declaration."""
    spec = describe("field_read.compress_breakeven_tokens")
    fr_params = FEATURE_META["field_read"]["params"]
    cbt = fr_params["compress_breakeven_tokens"]
    assert spec.preset == cbt["default"]
    assert spec.vmin == float(cbt["min"])
    assert spec.vmax == float(cbt["max"])


def test_compress_breakeven_source_points_to_feature_meta() -> None:
    """Source field documents the dedup so future readers see it."""
    spec = describe("field_read.compress_breakeven_tokens")
    assert "feature_config" in spec.source
    assert "FEATURE_META" in spec.source
