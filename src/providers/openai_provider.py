"""Thin wrapper around the OpenAI API for the review pipeline."""

from openai import OpenAI

from src.config import LLM_REQUEST_TIMEOUT_SECONDS


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
          max_tokens: int = 8192) -> str:
    """Single-turn call. Returns raw text (caller parses JSON).

    STREAMING, not a single blocking create() call - see anthropic_provider.py
    for the production incident this pattern fixes (a long non-streaming
    request got killed well past our configured client-side timeout, by
    infrastructure that specifically targets long non-streaming calls).
    Same risk applies here: a large chunk + large max_tokens can legitimately
    take minutes to generate. Accumulating from stream=True deltas is the
    traditional, most broadly SDK-version-compatible way to do this (rather
    than the newer .stream() helper, whose exact convenience-method surface
    has shifted across openai package versions).
    """
    client = OpenAI(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
    stream = client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
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
                     dynamic_content: str, max_tokens: int = 8192) -> str:
    """Same interface as the Anthropic provider's call_with_cache(), for a
    consistent llm_client.py surface across providers. Unlike Anthropic,
    OpenAI needs no explicit cache directive - it automatically caches the
    matching prefix of a request (system message + leading identical user
    content) across repeated calls, as long as that prefix is >=1024 tokens
    and calls happen within its cache window. The only thing OUR code needs
    to get right is keeping the same block order/content every call, which
    this does by construction. Delegates to call() above, which streams.
    """
    user_content = f"{cacheable_context}\n\n{dynamic_content}" if cacheable_context else dynamic_content
    return call(api_key, model, system_prompt, user_content, max_tokens)


def validate_key(api_key: str, model: str) -> tuple[bool, str]:
    """Short timeout - this is a quick UI feedback check, not a real review call."""
    try:
        client = OpenAI(api_key=api_key, timeout=20.0)
        client.chat.completions.create(
            model=model,
            max_completion_tokens=8,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
