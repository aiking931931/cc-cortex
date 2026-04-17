"""Pre-RoPE KV quantization — monkey-patch apply_rotary_pos_emb.

Validated on Llama-3.1-8B, LongBench hotpotqa, 10 examples @ 4-bit uniform::

    fp16 CE   = 1.478
    preRoPE CE = 1.518   (gap +0.04)
    post-RoPE CE = 8.57  (gap +7 — K rotation destroys quantization)

Why pre-RoPE:
  RoPE rotates K channels pair-wise (cos/sin mix). Rotated K has no clean
  per-channel structure → 4-bit symmetric quant blows up. Quantizing BEFORE
  rotation preserves KVQuant (2024) / KIVI's design invariant. 2025 SOTA
  (KVTuner, KITTY, MixKVQ) all use pre-RoPE K.

Why monkey-patch:
  Upstream transformers has no public hook point between "K computed" and
  "RoPE applied". We intercept ``modeling_llama.apply_rotary_pos_emb`` which
  is the single funnel for Llama attention. Uses ``*args, **kwargs`` so the
  patch survives the transformers 4.x → 5.x signature change (position_ids
  moved out of positional args).

Scope:
  - Llama-family only (Llama-2, Llama-3, Llama-3.1, Llama-3.2). Mistral /
    Qwen / Gemma use their own apply_rotary_pos_emb in different modules —
    extend by adding more ``install_pre_rope_kv_quant_<family>`` helpers.
  - K-only. V is not rotated; V quantization happens at cache-write time and
    is the consumer's job (not part of this monkey-patch).
"""

from __future__ import annotations

from typing import Any, Callable


def _qdq_perchannel(k: Any, bits: int) -> Any:
    """Per-channel symmetric quant-dequant on the head_dim axis.

    K shape: ``[B, H, T, D]`` — reduce absmax over the sequence axis (dim=2),
    keeping one scale per (batch, head, channel). This is the KVQuant / KIVI
    "per-channel K" recipe.

    Done in fp32 for numerical stability, then cast back to K's dtype.
    """
    import torch  # local import — torch is NOT a concinno core dep

    if not isinstance(k, torch.Tensor):  # pragma: no cover — defensive
        raise TypeError(f"_qdq_perchannel expects torch.Tensor, got {type(k)!r}")

    k_fp = k.float()
    qmax = (1 << (bits - 1)) - 1
    absmax = k_fp.abs().amax(dim=2, keepdim=True)  # [B, H, 1, D]
    scale = (absmax / max(qmax, 1)).clamp(min=1e-10)
    q = (k_fp / scale).round().clamp(-qmax, qmax)
    return (q * scale).to(k.dtype)


def install_pre_rope_kv_quant(
    bits: int = 4,
    enabled: bool = True,
) -> Callable[[], None]:
    """Monkey-patch Llama ``apply_rotary_pos_emb`` to quant K pre-RoPE.

    Args:
        bits: Quantization bit-width (4 recommended, 8 for safety).
        enabled: If False, install the patch but gate it off (flip via the
            returned ``uninstall`` callable? no — re-install with ``enabled=True``).
            This lets consumers A/B test fp16 vs quant without re-importing.

    Returns:
        Uninstall callable. Calling it restores the original
        ``apply_rotary_pos_emb`` reference. Idempotent: calling twice is a
        no-op on the second call.

    Raises:
        ImportError: if ``transformers`` is not installed.
        ValueError: if ``bits`` is not in 1..16.

    Example::

        from concinno.compression import install_pre_rope_kv_quant

        uninstall = install_pre_rope_kv_quant(bits=4)
        try:
            outputs = model(input_ids, use_cache=True)
            # K in past_key_values is now quant-dequant'd per-channel pre-RoPE
        finally:
            uninstall()
    """
    if not (1 <= bits <= 16):
        raise ValueError(f"bits must be in 1..16, got {bits}")

    try:
        from transformers.models.llama import modeling_llama
    except ImportError as exc:  # pragma: no cover — import guard
        raise ImportError(
            "install_pre_rope_kv_quant requires `transformers` "
            "(pip install transformers). Concinno core has no torch dep."
        ) from exc

    original = modeling_llama.apply_rotary_pos_emb
    state = {"enabled": enabled, "bits": bits, "uninstalled": False}

    def patched(q: Any, k: Any, *args: Any, **kwargs: Any) -> Any:
        if state["enabled"]:
            k = _qdq_perchannel(k, state["bits"])
        return original(q, k, *args, **kwargs)

    # Preserve original for introspection
    patched.__wrapped__ = original  # type: ignore[attr-defined]
    patched.__concinno_pre_rope_state__ = state  # type: ignore[attr-defined]

    modeling_llama.apply_rotary_pos_emb = patched

    def uninstall() -> None:
        if state["uninstalled"]:
            return
        # Only restore if we're still the active patch — don't clobber a
        # later patch that wrapped ours.
        if modeling_llama.apply_rotary_pos_emb is patched:
            modeling_llama.apply_rotary_pos_emb = original
        state["uninstalled"] = True
        state["enabled"] = False

    return uninstall
