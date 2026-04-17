"""concinno.compression — KV cache and weight compression primitives.

Validated primitives extracted from CBUA KV-compression research.

Public API::

    from concinno.compression import install_pre_rope_kv_quant

    uninstall = install_pre_rope_kv_quant(bits=4)
    # ... run model with quantized K pre-RoPE ...
    uninstall()

Consumer is responsible for importing transformers and running the model.
concinno.compression only installs the monkey-patch; zero runtime deps here.
"""

from concinno.compression.kv_prerope import (
    _qdq_perchannel,
    install_pre_rope_kv_quant,
)

__all__ = [
    "install_pre_rope_kv_quant",
    "_qdq_perchannel",
]
