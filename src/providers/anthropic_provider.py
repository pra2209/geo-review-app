"""Thin wrapper around the Anthropic API for the review pipeline."""

from anthropic import Anthropic


def call(api_key: str, model: str, system_prompt: str, user_prompt: str,
          max_tokens: int = 8192) -> str:
    """Single-turn call. Returns raw text (caller parses JSON)."""
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )


def validate_key(api_key: str, model: str) -> tuple[bool, str]:
    """Cheap round-trip to confirm the key/model combo actually works."""
    try:
        client = Anthropic(api_key=api_key)
        client.messages.create(
            model=model,
            max_tokens=8,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001 - surface any provider error to the UI
        return False, str(exc)
