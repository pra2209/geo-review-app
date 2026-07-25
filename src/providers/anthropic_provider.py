"""Thin wrapper around the Anthropic API for the review pipeline."""

from anthropic import Anthropic

from src.config import LLM_REQUEST_TIMEOUT_SECONDS


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
          max_tokens: int = 8192) -> str:
    """Single-turn call. Returns raw text (caller parses JSON)."""
    client = Anthropic(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )


def call_with_cache(api_key: str, model: str, system_prompt: str, cacheable_context: str,
                     dynamic_content: str, max_tokens: int = 8192) -> str:
    """Like call(), but marks the two large blocks that repeat byte-identically
    across many calls within one review job - the framework instructions
    (system_prompt) and the document digest (cacheable_context) - as
    ephemeral cache breakpoints. Anthropic only actually caches a block once
    it's at least ~1024 tokens (Sonnet/Opus tier, which is all our model
    allowlist contains); shorter content is sent normally with no error, it
    just won't be cached. Cache entries live ~5 minutes, comfortably longer
    than the few seconds between the chunk calls in one job's first pass.
    """
    client = Anthropic(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    content_blocks = []
    if cacheable_context:
        content_blocks.append({
            "type": "text", "text": cacheable_context,
            "cache_control": {"type": "ephemeral"},
        })
    content_blocks.append({"type": "text", "text": dynamic_content})

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text", "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": content_blocks}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )


def validate_key(api_key: str, model: str) -> tuple[bool, str]:
    """Cheap round-trip to confirm the key/model combo actually works. Short
    timeout - this is a quick UI feedback check, not a real review call."""
    try:
        client = Anthropic(api_key=api_key, timeout=20.0)
        client.messages.create(
            model=model,
            max_tokens=8,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001 - surface any provider error to the UI
        return False, str(exc)
