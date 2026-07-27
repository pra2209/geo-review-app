"""Regression/property tests for bounded PDF prompt planning and adaptive recovery."""

import json
import os
import random
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep these pure pipeline tests runnable in a minimal environment without
# contacting provider APIs. Production installs the real SDKs from requirements.
if "anthropic" not in sys.modules:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        module = types.ModuleType("anthropic")
        module.Anthropic = object
        sys.modules["anthropic"] = module

from src.config import REVIEW_MAX_TOKENS, REVIEW_RETRY_MIN_TOKENS
from src.llm_client import LLMConfig
from src.pdf_processing import Page, build_digest, chunk_pages, render_pages_as_text
from src.review_pipeline import _process_one_chunk

FAKE_CONFIG = LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="fake")


def _finding(snapshot: str, page: str = "Page 1") -> str:
    return json.dumps([{
        "source_file": "report.pdf",
        "page_or_item": page,
        "section_no": "1",
        "snapshot": snapshot,
        "discipline": "Geotechnical",
        "review_comment": "Review comment.",
        "status": "B",
        "framework_step": 1,
    }])


def test_single_dense_page_is_split_losslessly_and_never_exceeds_budget():
    text = "Dense laboratory table row with values 10 20 30.\n" * 600
    budget = 5_000
    chunks = chunk_pages([Page("dense.pdf", 7, text)], char_budget=budget, max_pages=12)

    assert len(chunks) > 1, "one over-budget PDF page must not bypass chunking"
    fragments = [page for chunk in chunks for page in chunk]
    assert "".join(page.text for page in fragments) == text
    assert all(page.source_file == "dense.pdf" and page.page_num == 7 for page in fragments)
    assert all(len(render_pages_as_text(chunk)) <= budget for chunk in chunks)
    assert [page.part_num for page in fragments] == list(range(1, len(fragments) + 1))
    assert all(page.part_count == len(fragments) for page in fragments)


def test_chunks_respect_fragment_limit_and_never_mix_source_files():
    pages = [Page("a.pdf", i, "a" * 20) for i in range(1, 8)]
    pages += [Page("b.pdf", i, "b" * 20) for i in range(1, 5)]
    chunks = chunk_pages(pages, char_budget=50_000, max_pages=3)

    assert all(len(chunk) <= 3 for chunk in chunks)
    assert all(len({page.source_file for page in chunk}) == 1 for chunk in chunks)
    assert [page.page_num for chunk in chunks for page in chunk if page.source_file == "a.pdf"] == list(range(1, 8))


def test_digest_is_size_bounded_and_allocates_context_to_every_file():
    pages = []
    for file_no in range(1, 6):
        for page_no in range(1, 4):
            pages.append(Page(f"file-{file_no}.pdf", page_no, str(file_no) * 8_000))

    digest = build_digest(
        pages,
        pages_per_file=20,
        total_char_budget=30_000,
        per_file_char_budget=12_000,
    )
    assert len(render_pages_as_text(digest)) <= 30_000
    assert {page.source_file for page in digest} == {f"file-{i}.pdf" for i in range(1, 6)}


def test_timeout_on_large_chunk_splits_instead_of_repeating_same_payload():
    chunk = [Page("report.pdf", 1, "A" * 5_000), Page("report.pdf", 2, "B" * 5_000)]
    calls = []

    class APITimeoutError(RuntimeError):
        pass

    def fake_call(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192, response_mode="balanced"):
        calls.append((dynamic_content, max_tokens))
        if len(calls) == 1:
            raise APITimeoutError("Request timed out or interrupted")
        return _finding(f"child-{len(calls) - 1}")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call):
        findings, error, timing = _process_one_chunk(
            1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0,
        )

    assert error is None
    assert len(findings) == 2
    assert len(calls) == 3
    assert calls[0][1] == REVIEW_MAX_TOKENS
    assert calls[1][1] == calls[2][1] == max(REVIEW_RETRY_MIN_TOKENS, REVIEW_MAX_TOKENS // 2)
    assert calls[0][0] != calls[1][0] != calls[2][0]
    assert timing["adaptive_splits"] == 1
    assert timing["attempts"] == 3


def test_second_level_timeout_is_recursively_split_with_bounded_calls():
    chunk = [Page("report.pdf", i, chr(64 + i) * 4_000) for i in range(1, 5)]
    calls = []

    class APITimeoutError(RuntimeError):
        pass

    def fake_call(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192, response_mode="balanced"):
        calls.append(dynamic_content)
        # Root and its left child time out. The left child must split again;
        # the other leaves succeed.
        if len(calls) in (1, 2):
            raise APITimeoutError("timeout")
        return _finding(f"success-{len(calls)}")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call):
        findings, error, timing = _process_one_chunk(
            1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0,
        )

    assert error is None
    assert len(findings) == 3
    assert timing["adaptive_splits"] == 2
    assert timing["attempts"] == 5


def test_small_unsplittable_timeout_retries_with_reduced_output_budget():
    max_tokens_seen = []

    class APITimeoutError(RuntimeError):
        pass

    def fake_call(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192, response_mode="balanced"):
        max_tokens_seen.append(max_tokens)
        if len(max_tokens_seen) == 1:
            raise APITimeoutError("timeout")
        return _finding("recovered")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call):
        findings, error, timing = _process_one_chunk(
            1, 1, [Page("report.pdf", 1, "short content")], FAKE_CONFIG,
            "framework", "digest", retry_delay_seconds=0,
        )

    assert error is None
    assert len(findings) == 1
    assert max_tokens_seen == [
        REVIEW_MAX_TOKENS,
        max(REVIEW_RETRY_MIN_TOKENS, REVIEW_MAX_TOKENS // 2),
    ]
    assert timing["adaptive_splits"] == 0


def test_partial_child_failure_keeps_successful_sibling_findings_and_warns():
    chunk = [Page("report.pdf", 1, "A" * 5_000), Page("report.pdf", 2, "B" * 5_000)]

    class APITimeoutError(RuntimeError):
        pass

    calls = {"count": 0}

    def fake_call(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192, response_mode="balanced"):
        calls["count"] += 1
        if calls["count"] == 1:
            raise APITimeoutError("timeout")
        if "chunk 1 of 1.1" in dynamic_content:
            return _finding("left-success")
        raise RuntimeError("right child provider failure")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call):
        findings, error, timing = _process_one_chunk(
            1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0,
        )

    assert [finding.snapshot for finding in findings] == ["left-success"]
    assert error is not None
    assert timing["partial"] is True
    assert error["partial_findings_kept"] == 1


def test_randomized_chunking_is_lossless_bounded_and_source_isolated():
    rng = random.Random(20260726)
    for _ in range(500):
        budget = rng.randint(500, 8_000)
        page_limit = rng.randint(1, 10)
        pages = []
        originals = {}
        for source_no in range(rng.randint(1, 4)):
            source = f"source-{source_no}-{'x' * rng.randint(0, 50)}.pdf"
            text_parts = []
            for page_no in range(1, rng.randint(2, 12)):
                text = "".join(rng.choice("abc \n.,;0123456789") for _ in range(rng.randint(0, 12_000)))
                pages.append(Page(source, page_no, text))
                text_parts.append(text)
            originals[source] = "".join(text_parts)

        chunks = chunk_pages(pages, char_budget=budget, max_pages=page_limit)
        assert all(len(render_pages_as_text(chunk)) <= budget for chunk in chunks)
        assert all(len(chunk) <= page_limit for chunk in chunks)
        assert all(len({page.source_file for page in chunk}) <= 1 for chunk in chunks)
        for source, expected in originals.items():
            actual = "".join(page.text for chunk in chunks for page in chunk if page.source_file == source)
            assert actual == expected


def test_thinking_only_large_chunk_uses_direct_retry_before_splitting():
    chunk = [Page("report.pdf", 13, "A" * 5_000), Page("report.pdf", 14, "B" * 5_000)]
    calls = []

    class NoTextResponseError(RuntimeError):
        pass

    def fake_call(config, system_prompt, cacheable_context, dynamic_content,
                  max_tokens=8192, response_mode="balanced"):
        calls.append((dynamic_content, max_tokens, response_mode))
        if len(calls) == 1:
            raise NoTextResponseError(
                "Anthropic returned no text content block; only thinking content block"
            )
        return _finding("direct-recovery")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call):
        findings, error, timing = _process_one_chunk(
            2, 3, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0,
        )

    assert error is None
    assert [finding.snapshot for finding in findings] == ["direct-recovery"]
    assert timing["adaptive_splits"] == 0
    assert calls == [
        (calls[0][0], REVIEW_MAX_TOKENS, "balanced"),
        (calls[0][0], REVIEW_MAX_TOKENS, "direct"),
    ]


def test_unsplittable_thinking_only_retry_switches_to_direct_output_mode():
    calls = []

    class NoTextResponseError(RuntimeError):
        pass

    def fake_call(config, system_prompt, cacheable_context, dynamic_content,
                  max_tokens=8192, response_mode="balanced"):
        calls.append((max_tokens, response_mode))
        if len(calls) == 1:
            raise NoTextResponseError(
                "Anthropic returned no text content block; only thinking content block"
            )
        return _finding("direct-recovery")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call):
        findings, error, timing = _process_one_chunk(
            1, 1, [Page("report.pdf", 27, "short text")], FAKE_CONFIG,
            "framework", "digest", retry_delay_seconds=0,
        )

    assert error is None
    assert [finding.snapshot for finding in findings] == ["direct-recovery"]
    assert calls == [
        (REVIEW_MAX_TOKENS, "balanced"),
        (REVIEW_MAX_TOKENS, "direct"),
    ]
    assert timing["attempts"] == 2
