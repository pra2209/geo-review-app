"""Thin wrapper around the Anthropic API for the review pipeline.

Claude Sonnet 5 enables adaptive thinking by default. Thinking and visible text
share ``max_tokens``, so a structured-output workload can otherwise consume the
entire response budget before emitting its JSON answer. This wrapper sets an
explicit balanced effort for normal review calls and supports a direct-output
fallback used by the adaptive recovery path.
"""

from anthropic import Anthropic

from src.config import LLM_REQUEST_TIMEOUT_SECONDS


class NoTextResponseError(RuntimeError):
    """Anthropic completed a request without returning user-visible text."""


_ADAPTIVE_THINKING_MODELS = {
    "claude-sonnet-5",
    "claude-opus-5",
}

_FINDINGS_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "source_file": {"type": "string"},
            "page_or_item": {"type": "string"},
            "section_no": {"type": "string"},
            "snapshot": {"type": "string"},
            "discipline": {"type": "string"},
            "review_comment": {"type": "string"},
            "status": {"type": "string", "enum": ["A", "B", "C", "D", "E"]},
            "framework_step": {"type": "integer"},
        },
        "required": [
            "source_file", "page_or_item", "section_no", "snapshot",
            "discipline", "review_comment", "status", "framework_step",
        ],
        "additionalProperties": False,
    },
}


def _thinking_controls(model: str, response_mode: str) -> dict:
    """Return model-compatible thinking controls for one request."""
    if model not in _ADAPTIVE_THINKING_MODELS:
        return {}
    if response_mode == "direct":
        return {
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "medium"},
        }
    if response_mode != "balanced":
        raise ValueError(f"Unknown Anthropic response_mode: {response_mode}")
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }


def _review_output_controls(model: str, response_mode: str) -> dict:
    """Combine thinking controls with a strict findings-array JSON schema."""
    controls = _thinking_controls(model, response_mode)
    output_config = dict(controls.get("output_config") or {})
    output_config["format"] = {
        "type": "json_schema",
        "schema": _FINDINGS_JSON_SCHEMA,
    }
    return {**controls, "output_config": output_config}


def _get_final_text_with_metadata(stream) -> str:
    """Return all text blocks or raise an actionable no-text response error.

    ``get_final_text()`` raises a generic RuntimeError when Claude returns only
    thinking blocks. Reading the final Message first preserves ``stop_reason``,
    content-block types and token usage, which are essential for deciding
    whether to split/retry and for explaining the gap to the user.
    """
    message = stream.get_final_message()
    text = "".join(
        block.text
        for block in message.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    )
    if text:
        return text

    block_types = [
        getattr(block, "type", type(block).__name__)
        for block in message.content
    ]
    stop_reason = getattr(message, "stop_reason", None)
    usage = getattr(message, "usage", None)
    output_tokens = getattr(usage, "output_tokens", None) if usage is not None else None
    raise NoTextResponseError(
        "Anthropic returned no text content block "
        f"(content_blocks={block_types or ['none']}, stop_reason={stop_reason!r}, "
        f"output_tokens={output_tokens!r}). The generated-token budget was likely "
        "consumed by adaptive thinking before the JSON answer was emitted."
    )


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
         max_tokens: int = 8192, response_mode: str = "balanced") -> str:
    """Single-turn streaming call returning raw text for caller-side parsing."""
    client = Anthropic(
        api_key=api_key,
        timeout=LLM_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        **_thinking_controls(model, response_mode),
    ) as stream:
        return _get_final_text_with_metadata(stream)


def call_with_cache(api_key: str, model: str, system_prompt: str,
                    cacheable_context: str, dynamic_content: str,
                    max_tokens: int = 8192,
                    response_mode: str = "balanced") -> str:
    """Streaming call with Anthropic prompt-cache breakpoints.

    The framework and digest repeat byte-identically across chunk calls; the
    detailed chunk remains uncached. SDK retries are disabled because retry and
    adaptive splitting are owned by the application pipeline.
    """
    client = Anthropic(
        api_key=api_key,
        timeout=LLM_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    content_blocks = []
    if cacheable_context:
        content_blocks.append({
            "type": "text",
            "text": cacheable_context,
            "cache_control": {"type": "ephemeral"},
        })
    content_blocks.append({"type": "text", "text": dynamic_content})

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": content_blocks}],
        **_review_output_controls(model, response_mode),
    ) as stream:
        return _get_final_text_with_metadata(stream)


def validate_key(api_key: str, model: str) -> tuple[bool, str]:
    """Cheap round-trip to confirm that the key/model combination works."""
    try:
        client = Anthropic(api_key=api_key, timeout=20.0, max_retries=0)
        client.messages.create(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with OK."}],
            **_thinking_controls(model, "direct"),
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001 - surface provider error to UI
        return False, str(exc)
