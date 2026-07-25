"""Verifies the ACTUAL request payload our code builds carries the right
prompt-caching directives - without needing a real API key or network call.
Mocks the provider SDK client and inspects the arguments it was called with.

Run with: python -m pytest tests/ (or just: python tests/test_prompt_caching.py)
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.providers import anthropic_provider, openai_provider


def _fake_anthropic_response(text="[]"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def test_anthropic_call_with_cache_marks_system_and_context_as_ephemeral():
    with patch("src.providers.anthropic_provider.Anthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create.return_value = _fake_anthropic_response()

        anthropic_provider.call_with_cache(
            api_key="fake-key", model="claude-sonnet-5",
            system_prompt="FRAMEWORK INSTRUCTIONS (repeats every call)",
            cacheable_context="DIGEST TEXT (repeats every call)",
            dynamic_content="chunk 1 of 4 specific content",
            max_tokens=1234,
        )

        call_kwargs = instance.messages.create.call_args.kwargs

        # System prompt must be a cache-marked content block, not a plain string,
        # or Anthropic has nothing to attach the cache breakpoint to.
        system = call_kwargs["system"]
        assert isinstance(system, list), "system must be block list to carry cache_control"
        assert system[0]["text"] == "FRAMEWORK INSTRUCTIONS (repeats every call)"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

        # The digest block must ALSO be cache-marked and come before the
        # dynamic (uncached) chunk content in the same message.
        content_blocks = call_kwargs["messages"][0]["content"]
        assert content_blocks[0]["text"] == "DIGEST TEXT (repeats every call)"
        assert content_blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert content_blocks[1]["text"] == "chunk 1 of 4 specific content"
        assert "cache_control" not in content_blocks[1], "dynamic content must NOT be cached"

        assert call_kwargs["max_tokens"] == 1234
        print("test_anthropic_call_with_cache_marks_system_and_context_as_ephemeral: OK")


def test_anthropic_call_with_cache_is_stable_across_repeated_calls():
    """The whole point of caching is that the cacheable prefix is BYTE-IDENTICAL
    call after call. Simulate two chunk calls (like run_first_pass's loop) and
    assert the system + context blocks sent are identical both times, while
    only dynamic_content differs."""
    with patch("src.providers.anthropic_provider.Anthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create.return_value = _fake_anthropic_response()

        for i, chunk_text in enumerate(["chunk one text", "chunk two text"], start=1):
            anthropic_provider.call_with_cache(
                api_key="fake-key", model="claude-sonnet-5",
                system_prompt="SAME FRAMEWORK EVERY TIME",
                cacheable_context="SAME DIGEST EVERY TIME",
                dynamic_content=f"chunk {i}: {chunk_text}",
            )

        calls = instance.messages.create.call_args_list
        assert calls[0].kwargs["system"] == calls[1].kwargs["system"], \
            "system block changed between calls - cache would miss"
        assert calls[0].kwargs["messages"][0]["content"][0] == calls[1].kwargs["messages"][0]["content"][0], \
            "cacheable context block changed between calls - cache would miss"
        assert calls[0].kwargs["messages"][0]["content"][1] != calls[1].kwargs["messages"][0]["content"][1], \
            "dynamic content should differ per chunk"
        print("test_anthropic_call_with_cache_is_stable_across_repeated_calls: OK")


def test_openai_call_with_cache_preserves_prefix_order():
    """OpenAI's caching is automatic and prefix-based - our job is just to
    keep the same block order/content every call. Verify the concatenation
    puts cacheable_context first, dynamic_content second, unchanged."""
    with patch("src.providers.openai_provider.OpenAI") as MockClient:
        instance = MockClient.return_value
        msg = MagicMock()
        msg.message.content = "[]"
        resp = MagicMock()
        resp.choices = [msg]
        instance.chat.completions.create.return_value = resp

        openai_provider.call_with_cache(
            api_key="fake-key", model="gpt-4.1",
            system_prompt="FRAMEWORK",
            cacheable_context="DIGEST",
            dynamic_content="CHUNK 1",
        )

        call_kwargs = instance.chat.completions.create.call_args.kwargs
        user_message = call_kwargs["messages"][1]["content"]
        assert user_message.startswith("DIGEST"), "cacheable context must come first"
        assert user_message.endswith("CHUNK 1"), "dynamic content must come last"
        print("test_openai_call_with_cache_preserves_prefix_order: OK")


if __name__ == "__main__":
    test_anthropic_call_with_cache_marks_system_and_context_as_ephemeral()
    test_anthropic_call_with_cache_is_stable_across_repeated_calls()
    test_openai_call_with_cache_preserves_prefix_order()
    print("All tests passed.")
