"""Verifies the ACTUAL request payload our code builds carries the right
prompt-caching directives - without needing a real API key or network call.
Mocks the provider SDK client and inspects the arguments it was called with.

All three providers stream now (see anthropic_provider.py module docstring
for the production incident - a long non-streaming call got killed well past
our configured timeout - that this fixes), so the mocks here simulate a
streaming response rather than a single blocking one.

Run with: python -m pytest tests/ (or just: python tests/test_prompt_caching.py)
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.providers import anthropic_provider, openai_provider, gemini_provider


def _mock_anthropic_stream(instance, text="[]"):
    """client.messages.stream(...) is a context manager; stream.get_final_text()
    returns the accumulated text. Configure the mock to behave that way."""
    stream_cm = MagicMock()
    stream_cm.__enter__.return_value.get_final_text.return_value = text
    instance.messages.stream.return_value = stream_cm


def _mock_openai_style_stream(instance, text="[]"):
    """client.chat.completions.create(..., stream=True) returns an iterable
    of chunks, each carrying one piece of the response in .choices[0].delta.content.
    A single chunk carrying the whole text is sufficient for these tests -
    they check the REQUEST payload, not chunk-by-chunk accumulation (that's
    covered separately, see test_streaming_accumulation.py)."""
    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content=text))]
    instance.chat.completions.create.return_value = [chunk]


def test_anthropic_call_with_cache_marks_system_and_context_as_ephemeral():
    with patch("src.providers.anthropic_provider.Anthropic") as MockClient:
        instance = MockClient.return_value
        _mock_anthropic_stream(instance)

        anthropic_provider.call_with_cache(
            api_key="fake-key", model="claude-sonnet-5",
            system_prompt="FRAMEWORK INSTRUCTIONS (repeats every call)",
            cacheable_context="DIGEST TEXT (repeats every call)",
            dynamic_content="chunk 1 of 4 specific content",
            max_tokens=1234,
        )

        assert MockClient.call_args.kwargs["max_retries"] == 0, \
            "application retry logic must be the only retry owner"
        call_kwargs = instance.messages.stream.call_args.kwargs

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
        _mock_anthropic_stream(instance)

        for i, chunk_text in enumerate(["chunk one text", "chunk two text"], start=1):
            anthropic_provider.call_with_cache(
                api_key="fake-key", model="claude-sonnet-5",
                system_prompt="SAME FRAMEWORK EVERY TIME",
                cacheable_context="SAME DIGEST EVERY TIME",
                dynamic_content=f"chunk {i}: {chunk_text}",
            )

        calls = instance.messages.stream.call_args_list
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
        _mock_openai_style_stream(instance)

        openai_provider.call_with_cache(
            api_key="fake-key", model="gpt-4.1",
            system_prompt="FRAMEWORK",
            cacheable_context="DIGEST",
            dynamic_content="CHUNK 1",
        )

        assert MockClient.call_args.kwargs["max_retries"] == 0, \
            "application retry logic must be the only retry owner"
        call_kwargs = instance.chat.completions.create.call_args.kwargs
        assert call_kwargs["stream"] is True, "must request a streaming response"
        user_message = call_kwargs["messages"][1]["content"]
        assert user_message.startswith("DIGEST"), "cacheable context must come first"
        assert user_message.endswith("CHUNK 1"), "dynamic content must come last"
        print("test_openai_call_with_cache_preserves_prefix_order: OK")


def test_gemini_uses_google_endpoint_not_openais():
    """Guards against the one plausible copy-paste bug in this provider:
    forgetting the custom base_url and silently sending Gemini-labeled calls
    to OpenAI's actual API (which would just fail with an unknown-model
    error at best, or - worse - quietly charge the wrong account if a
    fallback key were ever present)."""
    with patch("src.providers.gemini_provider.OpenAI") as MockClient:
        _mock_openai_style_stream(MockClient.return_value)
        gemini_provider.call(api_key="fake-gemini-key", model="gemini-2.5-pro",
                              system_prompt="sys", user_prompt="usr")
        _, kwargs = MockClient.call_args
        assert kwargs["max_retries"] == 0
        assert kwargs["base_url"] == gemini_provider.GEMINI_BASE_URL
        assert "openai.com" not in kwargs["base_url"]
        print("test_gemini_uses_google_endpoint_not_openais: OK")


def test_gemini_call_with_cache_preserves_prefix_order():
    with patch("src.providers.gemini_provider.OpenAI") as MockClient:
        instance = MockClient.return_value
        _mock_openai_style_stream(instance)

        gemini_provider.call_with_cache(
            api_key="fake-key", model="gemini-2.5-pro",
            system_prompt="FRAMEWORK", cacheable_context="DIGEST", dynamic_content="CHUNK 1",
        )

        call_kwargs = instance.chat.completions.create.call_args.kwargs
        assert call_kwargs["stream"] is True, "must request a streaming response"
        user_message = call_kwargs["messages"][1]["content"]
        assert user_message.startswith("DIGEST")
        assert user_message.endswith("CHUNK 1")
        print("test_gemini_call_with_cache_preserves_prefix_order: OK")


if __name__ == "__main__":
    test_anthropic_call_with_cache_marks_system_and_context_as_ephemeral()
    test_anthropic_call_with_cache_is_stable_across_repeated_calls()
    test_openai_call_with_cache_preserves_prefix_order()
    test_gemini_uses_google_endpoint_not_openais()
    test_gemini_call_with_cache_preserves_prefix_order()
    print("All tests passed.")
