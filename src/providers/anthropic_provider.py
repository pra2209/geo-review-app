"""Thin wrapper around the Anthropic API for the review pipeline."""

from anthropic import Anthropic

from src.config import LLM_REQUEST_TIMEOUT_SECONDS


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
          max_tokens: int = 8192) -> str:
    """Single-turn call. Returns raw text (caller parses JSON).

    STREAMING, not a single blocking create() call - see the module-level
    note in call_with_cache() below for why this matters; a real production
    failure (908s duration against a 150s configured timeout, on a chunk
    whose content required a large synthesized response) traced directly to
    this NOT being the case previously.
    """
    client = Anthropic(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        return stream.get_final_text()


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

    Uses client.messages.stream() rather than messages.create(). A real
    incident traced to this: a single large chunk (an 80k-char document
    section with a big framework system prompt, requesting up to
    REVIEW_MAX_TOKENS of structured output) took long enough to generate
    that the connection was terminated - NOT by our configured
    LLM_REQUEST_TIMEOUT_SECONDS=150 (observed failure was at ~450s per
    attempt, 908.6s total across the one retry - our client-side timeout
    never fired), but by Anthropic's own handling of long-running
    NON-streaming requests (the error explicitly cited
    docs.anthropic.com/en/api/errors#long-requests). Streaming avoids this
    failure mode entirely - data flows continuously for the duration of
    generation - and, as a bonus, makes LLM_REQUEST_TIMEOUT_SECONDS mean
    what it always should have: "no data for this long = give up", not
    "the whole multi-minute generation must finish inside this window".
    """
    client = Anthropic(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    content_blocks = []
    if cacheable_context:
        content_blocks.append({
            "type": "text", "text": cacheable_context,
            "cache_control": {"type": "ephemeral"},
        })
    content_blocks.append({"type": "text", "text": dynamic_content})

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text", "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": content_blocks}],
    ) as stream:
        return stream.get_final_text()


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
