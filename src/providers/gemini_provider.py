"""Thin wrapper around Google's Gemini API, via its OpenAI-compatible endpoint.

Reuses the `openai` package (already a dependency) pointed at Google's
compatibility layer instead of writing a separate native-SDK integration -
lower risk, faster to ship, and easy to upgrade later if we want
Gemini-specific features (e.g. explicit context caching, response_schema
strict JSON mode) that the compat layer doesn't expose as cleanly.

Note: max_tokens (not OpenAI's newer max_completion_tokens) is used here
for the widest compatibility with Google's compat layer.
"""

from openai import OpenAI

from src.config import LLM_REQUEST_TIMEOUT_SECONDS

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _client(api_key: str, timeout: float = LLM_REQUEST_TIMEOUT_SECONDS) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL, timeout=timeout, max_retries=0)


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
          max_tokens: int = 8192, response_mode: str = "balanced") -> str:
    """Single-turn call. Returns raw text (caller parses JSON).

    STREAMING, not a single blocking create() call - see anthropic_provider.py
    for the production incident this pattern fixes (a long non-streaming
    request got killed well past our configured client-side timeout, by
    infrastructure that specifically targets long non-streaming calls). Same
    risk applies through Google's OpenAI-compat endpoint.
    """
    client = _client(api_key)
    stream = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        stream=True,
    )
    parts = []
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
    return "".join(parts)


def call_with_cache(api_key: str, model: str, system_prompt: str, cacheable_context: str,
                     dynamic_content: str, max_tokens: int = 8192, response_mode: str = "balanced") -> str:
    """Same interface as the other providers' call_with_cache(). Gemini 2.5+
    models apply caching automatically server-side for repeated prefixes
    (like OpenAI), so - same as the OpenAI provider - our job is just to keep
    the cacheable portion first and byte-identical across calls, not issue
    any explicit cache directive. If this later moves off the OpenAI-compat
    endpoint to Gemini's native SDK, Gemini also supports EXPLICIT caching
    (creating a CachedContent object with a TTL) for a stronger guarantee -
    worth revisiting if automatic caching doesn't hit reliably in practice.
    """
    user_content = f"{cacheable_context}\n\n{dynamic_content}" if cacheable_context else dynamic_content
    return call(api_key, model, system_prompt, user_content, max_tokens, response_mode=response_mode)


def validate_key(api_key: str, model: str) -> tuple[bool, str]:
    """Short timeout - this is a quick UI feedback check, not a real review call."""
    try:
        client = _client(api_key, timeout=20.0)
        client.chat.completions.create(
            model=model,
            max_tokens=8,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
