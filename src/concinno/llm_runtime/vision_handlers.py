"""Custom llama-cpp-python chat handlers for vision-capable local LLMs.

llama-cpp-python 0.3.20 ships ``Llava15ChatHandler`` /
``Qwen25VLChatHandler`` / ``MiniCPMv26ChatHandler`` / ``MoondreamChatHandler``
etc. but no Gemma-family handler, even though llama.cpp itself supports
Gemma 4 multimodal via the generic ``mtmd`` API (``llama-mtmd-cli``).

The image-encoding pipeline in ``Llava15ChatHandler.__init__`` uses
``mtmd_init_from_file(clip_path, llama_model, ctx_params)`` which is
*model-agnostic* — any mmproj GGUF in mtmd format works as long as we
feed the right chat template to the text model. So to wire Gemma 4
vision in-process we only need to subclass with Gemma's template.

Usage:

    from concinno.llm_runtime.vision_handlers import (
        Gemma4VisionChatHandler,
    )
    from llama_cpp import Llama

    handler = Gemma4VisionChatHandler(
        clip_model_path="/path/to/mmproj-gemma-4-31B-it-Q8_0.gguf",
        verbose=False,
    )
    llm = Llama(
        model_path="/path/to/gemma-4-31B-it-Q4_K_M.gguf",
        chat_handler=handler,
        n_ctx=8192,
        n_gpu_layers=-1,
    )
    resp = llm.create_chat_completion(messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What's in this image?"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }])

Minimum Concinno version: 2.21.0
"""
from __future__ import annotations


def get_gemma4_vision_handler_cls():
    """Return the :class:`Gemma4VisionChatHandler` class.

    llama-cpp-python is an optional dependency (``concinno[llm-local]``),
    so we defer the import until a caller actually asks for vision
    handler — keeps ``concinno.llm_runtime`` importable in deploys that
    don't ship the local-model stack.
    """
    from llama_cpp.llama_chat_format import Llava15ChatHandler

    class Gemma4VisionChatHandler(Llava15ChatHandler):
        """Gemma 4 vision chat handler — Gemma chat template + mtmd mmproj.

        Reuses :class:`Llava15ChatHandler`'s ``mtmd`` image encoding path
        (generic across mmproj GGUFs). Overrides the chat template with
        Gemma 4's ``<start_of_turn>`` / ``<end_of_turn>`` convention
        where ``assistant`` is rendered as the ``model`` role.
        """

        DEFAULT_SYSTEM_MESSAGE = None  # Gemma has no system role slot

        CHAT_FORMAT = (
            "{% for message in messages %}"
            # system prefix (optional) gets prepended to first user
            "{% if message['role'] == 'system' %}"
            "{{ message['content'] }}\n"
            "{% else %}"
            "<start_of_turn>"
            "{% if message['role'] == 'assistant' %}model"
            "{% else %}user{% endif %}\n"
            # body: plain string OR list of content blocks
            "{% if message['content'] is string %}"
            "{{ message['content'] }}"
            "{% else %}"
            "{% for content in message['content'] %}"
            "{% if content['type'] == 'image_url' %}"
            "{% if content.image_url is string %}"
            "{{ content.image_url }}"
            "{% else %}"
            "{{ content.image_url.url }}"
            "{% endif %}"
            "{% elif content['type'] == 'text' %}"
            "{{ content['text'] }}"
            "{% endif %}"
            "{% endfor %}"
            "{% endif %}"
            "<end_of_turn>\n"
            "{% endif %}"
            "{% endfor %}"
            "<start_of_turn>model\n"
        )

        def __call__(self, **kwargs):  # type: ignore[override]
            # Only reset state when this call actually carries an image.
            # Text-only multi-turn chat (e.g. agent gather-synth loop)
            # benefits from KV-cache reuse across turns — blanket reset
            # on every call regresses gather performance from ~10s to
            # ~170s and induces "insufficient evidence" responses.
            messages = kwargs.get("messages") or []
            has_image = False
            for m in messages:
                content = m.get("content") if isinstance(m, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "image_url"
                        ):
                            has_image = True
                            break
                if has_image:
                    break
            if has_image:
                llama = kwargs.get("llama")
                if llama is not None:
                    llama.reset()
                    llama._ctx.kv_cache_clear()
                    llama.n_tokens = 0
                    if hasattr(llama, "input_ids"):
                        llama.input_ids.fill(0)
                if hasattr(self, "_last_image_embed"):
                    self._last_image_embed = None
                    self._last_image_hash = None
            return super().__call__(**kwargs)

    return Gemma4VisionChatHandler


__all__ = ["get_gemma4_vision_handler_cls"]
