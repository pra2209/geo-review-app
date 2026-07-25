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
