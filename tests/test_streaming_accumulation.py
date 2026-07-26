"""Tests the manual chunk-accumulation logic in openai_provider.call() and
gemini_provider.call() - this is hand-written code (unlike the Anthropic
provider, which delegates accumulation to the SDK's own get_final_text()),
so it needs its own correctness test: multiple chunks must concatenate in
order, and chunks with no delta content (common in real streaming responses -
e.g. the first chunk often carries only role info, the last only a finish
reason) must be skipped without raising or inserting gaps/None.

Run with: python -m pytest tests/ (or just: python tests/test_streaming_accumulation.py)
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.providers import openai_provider, gemini_provider


def _fake_chunk(content):
    """Mimics one chunk of an OpenAI-style streaming response. content=None
    simulates a real chunk that carries no text (role-only opener, or the
    closing chunk with only a finish_reason) - these show up constantly in
    real streams and must not break accumulation."""
    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content=content))]
    return chunk


def test_openai_accumulates_multiple_chunks_in_order():
    with patch("src.providers.openai_provider.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = [
            _fake_chunk(None),      # role-only opener, common in real streams
            _fake_chunk("Hello"),
            _fake_chunk(", "),
            _fake_chunk("world"),
            _fake_chunk("!"),
            _fake_chunk(None),      # finish-reason-only closer
        ]

        result = openai_provider.call(
            api_key="fake-key", model="gpt-4.1",
            system_prompt="sys", user_prompt="usr",
        )

        assert result == "Hello, world!", f"expected clean concatenation, got: {result!r}"
        print("test_openai_accumulates_multiple_chunks_in_order: OK")


def test_openai_handles_empty_stream():
    """No chunks at all (e.g. an immediately-closed connection) must return
    an empty string, not raise."""
    with patch("src.providers.openai_provider.OpenAI") as MockClient:
        MockClient.return_value.chat.completions.create.return_value = []
        result = openai_provider.call(
            api_key="fake-key", model="gpt-4.1", system_prompt="sys", user_prompt="usr")
        assert result == ""
        print("test_openai_handles_empty_stream: OK")


def test_gemini_accumulates_multiple_chunks_in_order():
    """Same accumulation logic, independently implemented in gemini_provider.py -
    verify it too, not just that it looks similar to the OpenAI one."""
    with patch("src.providers.gemini_provider.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = [
            _fake_chunk(None),
            _fake_chunk('{"finding": '),
            _fake_chunk('"test"}'),
            _fake_chunk(None),
        ]

        result = gemini_provider.call(
            api_key="fake-key", model="gemini-2.5-pro",
            system_prompt="sys", user_prompt="usr",
        )

        assert result == '{"finding": "test"}', f"expected clean concatenation, got: {result!r}"
        print("test_gemini_accumulates_multiple_chunks_in_order: OK")


if __name__ == "__main__":
    test_openai_accumulates_multiple_chunks_in_order()
    test_openai_handles_empty_stream()
    test_gemini_accumulates_multiple_chunks_in_order()
    print("All tests passed.")
