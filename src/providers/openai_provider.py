"""Thin wrapper around the OpenAI API for the review pipeline."""

from openai import OpenAI


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
          max_tokens: int = 8192) -> str:
    """Single-turn call. Returns raw text (caller parses JSON)."""
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return resp.choices[0].message.content or ""


def call_with_cache(api_key: str, model: str, system_prompt: str, cacheable_context: str,
                     dynamic_content: str, max_tokens: int = 8192) -> str:
    """Same interface as the Anthropic provider's call_with_cache(), for a
    consistent llm_client.py surface across providers. Unlike Anthropic,
    OpenAI needs no explicit cache directive - it automatically caches the
    matching prefix of a request (system message + leading identical user
    content) across repeated calls, as long as that prefix is >=1024 tokens
    and calls happen within its cache window. The only thing OUR code needs
    to get right is keeping the same block order/content every call, which
    this does by construction.
    """
    user_content = f"{cacheable_context}\n\n{dynamic_content}" if cacheable_context else dynamic_content
    return call(api_key, model, system_prompt, user_content, max_tokens)


def validate_key(api_key: str, model: str) -> tuple[bool, str]:
    try:
        client = OpenAI(api_key=api_key)
        client.chat.completions.create(
            model=model,
            max_completion_tokens=8,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
