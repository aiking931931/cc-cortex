"""Tests for concinno.compression.kv_prerope — pre-RoPE KV quant monkey-patch."""

from __future__ import annotations

import sys
import types

import pytest

from concinno.compression import _qdq_perchannel, install_pre_rope_kv_quant

# ── Public API surface ──────────────────────────────────────


class TestAPIExists:
    def test_install_is_callable(self) -> None:
        assert callable(install_pre_rope_kv_quant)

    def test_qdq_perchannel_is_callable(self) -> None:
        assert callable(_qdq_perchannel)

    def test_default_bits_is_4(self) -> None:
        # Introspect default — signature spec says bits=4
        import inspect

        sig = inspect.signature(install_pre_rope_kv_quant)
        assert sig.parameters["bits"].default == 4
        assert sig.parameters["enabled"].default is True


class TestArgValidation:
    def test_bits_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="bits must be in"):
            install_pre_rope_kv_quant(bits=0)
        with pytest.raises(ValueError, match="bits must be in"):
            install_pre_rope_kv_quant(bits=17)

    def test_bits_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            install_pre_rope_kv_quant(bits=-1)


# ── Monkey-patch lifecycle (using a fake transformers module) ─


@pytest.fixture
def fake_transformers(monkeypatch: pytest.MonkeyPatch):
    """Stand in a minimal fake ``transformers.models.llama.modeling_llama``.

    Avoids pulling the real ~1GB transformers install just to test the
    install/uninstall cycle. The real integration test lives in
    experiments/cbua_plan_a/phase1p5_pre_rope_kvquant.py.
    """
    pkg = types.ModuleType("transformers")
    models = types.ModuleType("transformers.models")
    llama_pkg = types.ModuleType("transformers.models.llama")
    modeling = types.ModuleType("transformers.models.llama.modeling_llama")

    def original_apply_rotary_pos_emb(q, k, *args, **kwargs):
        return ("rotated_q", k, args, kwargs)

    modeling.apply_rotary_pos_emb = original_apply_rotary_pos_emb
    llama_pkg.modeling_llama = modeling
    models.llama = llama_pkg
    pkg.models = models

    monkeypatch.setitem(sys.modules, "transformers", pkg)
    monkeypatch.setitem(sys.modules, "transformers.models", models)
    monkeypatch.setitem(sys.modules, "transformers.models.llama", llama_pkg)
    monkeypatch.setitem(
        sys.modules, "transformers.models.llama.modeling_llama", modeling
    )
    return modeling


class TestInstallCycle:
    def test_install_replaces_apply_rotary(self, fake_transformers) -> None:
        original = fake_transformers.apply_rotary_pos_emb
        uninstall = install_pre_rope_kv_quant(bits=4)
        try:
            assert fake_transformers.apply_rotary_pos_emb is not original
            assert hasattr(
                fake_transformers.apply_rotary_pos_emb, "__concinno_pre_rope_state__"
            )
        finally:
            uninstall()

    def test_uninstall_restores_original(self, fake_transformers) -> None:
        original = fake_transformers.apply_rotary_pos_emb
        uninstall = install_pre_rope_kv_quant(bits=4)
        uninstall()
        assert fake_transformers.apply_rotary_pos_emb is original

    def test_uninstall_is_idempotent(self, fake_transformers) -> None:
        uninstall = install_pre_rope_kv_quant(bits=4)
        uninstall()
        uninstall()  # second call must not raise

    def test_disabled_flag_skips_quant(self, fake_transformers) -> None:
        # When enabled=False, the patch is installed but passes K through.
        uninstall = install_pre_rope_kv_quant(bits=4, enabled=False)
        try:
            state = fake_transformers.apply_rotary_pos_emb.__concinno_pre_rope_state__
            assert state["enabled"] is False
            assert state["bits"] == 4
        finally:
            uninstall()


# ── _qdq_perchannel numeric contract (torch optional) ────────

try:
    import torch

    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


@pytest.mark.skipif(not HAS_TORCH, reason="torch not installed in test env")
class TestQdqPerchannel:
    def test_shape_preserved(self) -> None:
        k = torch.randn(1, 4, 16, 32)  # [B, H, T, D]
        out = _qdq_perchannel(k, bits=4)
        assert out.shape == k.shape
        assert out.dtype == k.dtype

    def test_quant_is_bounded(self) -> None:
        # Dequantized values must not explode vs fp input.
        k = torch.randn(1, 2, 8, 16)
        out = _qdq_perchannel(k, bits=4)
        assert out.abs().max().item() <= k.abs().max().item() * 1.01

    def test_higher_bits_lower_error(self) -> None:
        torch.manual_seed(0)
        k = torch.randn(1, 2, 8, 16)
        err_4 = (_qdq_perchannel(k, bits=4) - k).abs().mean().item()
        err_8 = (_qdq_perchannel(k, bits=8) - k).abs().mean().item()
        assert err_8 < err_4, "8-bit quant should have lower error than 4-bit"

    def test_non_tensor_raises(self) -> None:
        with pytest.raises(TypeError):
            _qdq_perchannel([1.0, 2.0, 3.0], bits=4)
