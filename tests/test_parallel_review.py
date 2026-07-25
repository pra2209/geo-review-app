"""Tests for the parallelized run_first_pass: chunks are processed
concurrently for latency, but the output must still read in original
document order regardless of which chunk's API call happened to finish
first, and a transient failure should be retried once before giving up.

Run with: python -m pytest tests/ (or just: python tests/test_parallel_review.py)
"""

import json
import sys
import os
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pdf_processing import Page
from src.review_pipeline import run_first_pass, _process_one_chunk
from src.llm_client import LLMConfig

FAKE_CONFIG = LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="fake-key")


def _page(source_file, page_num, text):
    return Page(source_file=source_file, page_num=page_num, text=text)


def _finding_json(snapshot):
    return json.dumps([{
        "source_file": "report.pdf", "page_or_item": "Page 1", "section_no": "1",
        "snapshot": snapshot, "discipline": "Geotechnical",
        "review_comment": "Fine.", "status": "A", "framework_step": 1,
    }])


def test_run_first_pass_preserves_chunk_order_despite_reversed_completion():
    """Chunk 3 finishes FASTEST (simulated), chunk 1 SLOWEST - the opposite of
    submission order - to prove the final findings list is restored to
    document order and isn't just accidentally correct under low contention."""
    chunks = [
        [_page("report.pdf", 1, "chunk one content")],
        [_page("report.pdf", 2, "chunk two content")],
        [_page("report.pdf", 3, "chunk three content")],
    ]
    digest = [_page("report.pdf", 0, "digest content")]

    def fake_call_with_cache(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        if "chunk 1 of 3" in dynamic_content:
            time.sleep(0.15)  # slowest - finishes LAST despite being submitted first
            return _finding_json("Finding from chunk 1")
        if "chunk 2 of 3" in dynamic_content:
            time.sleep(0.08)
            return _finding_json("Finding from chunk 2")
        time.sleep(0.01)  # fastest - finishes FIRST despite being submitted last
        return _finding_json("Finding from chunk 3")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call_with_cache):
        findings, chunk_errors, chunk_timings = run_first_pass(
            FAKE_CONFIG, digest, chunks, progress_cb=None, max_workers=3)

    assert chunk_errors == []
    assert [f.snapshot for f in findings] == [
        "Finding from chunk 1", "Finding from chunk 2", "Finding from chunk 3",
    ], "findings must be in original document order, not completion order"
    assert len(chunk_timings) == 3
    print("test_run_first_pass_preserves_chunk_order_despite_reversed_completion: OK")


def test_run_first_pass_runs_concurrently_not_sequentially():
    """3 chunks, each simulated as taking 0.2s. Sequential would take >=0.6s;
    with max_workers=3 they should all run in parallel, finishing in well
    under that. This is the actual latency fix - assert it's real, not just
    that the code compiles."""
    chunks = [[_page("report.pdf", i, f"content {i}")] for i in range(1, 4)]
    digest = [_page("report.pdf", 0, "digest")]

    def fake_call_with_cache(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        time.sleep(0.2)
        return _finding_json("Finding")

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call_with_cache):
        start = time.perf_counter()
        findings, chunk_errors, chunk_timings = run_first_pass(
            FAKE_CONFIG, digest, chunks, progress_cb=None, max_workers=3)
        elapsed = time.perf_counter() - start

    assert len(findings) == 3
    assert elapsed < 0.5, f"expected concurrent execution well under 0.6s sequential time, took {elapsed:.2f}s"
    print(f"test_run_first_pass_runs_concurrently_not_sequentially: OK ({elapsed:.2f}s for 3x0.2s chunks)")


def test_process_one_chunk_retries_once_then_succeeds():
    chunk = [_page("report.pdf", 1, "content")]
    digest_calls = {"count": 0}

    def flaky_call(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        digest_calls["count"] += 1
        if digest_calls["count"] == 1:
            raise RuntimeError("simulated transient API error")
        return _finding_json("Recovered on retry")

    with patch("src.review_pipeline.call_with_cache", side_effect=flaky_call):
        findings, error, timing = _process_one_chunk(
            1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0.01)

    assert error is None, f"expected retry to succeed, got error: {error}"
    assert len(findings) == 1
    assert findings[0].snapshot == "Recovered on retry"
    assert timing["attempts"] == 2
    print("test_process_one_chunk_retries_once_then_succeeds: OK")


def test_process_one_chunk_gives_up_after_two_failures():
    chunk = [_page("report.pdf", 1, "content")]

    def always_fails(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        raise RuntimeError("persistent failure")

    with patch("src.review_pipeline.call_with_cache", side_effect=always_fails):
        findings, error, timing = _process_one_chunk(
            1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0.01)

    assert findings == []
    assert error is not None
    assert "failed after retry" in error["error"]
    assert timing["attempts"] == 2
    assert timing["failed"] is True
    print("test_process_one_chunk_gives_up_after_two_failures: OK")


if __name__ == "__main__":
    test_run_first_pass_preserves_chunk_order_despite_reversed_completion()
    test_run_first_pass_runs_concurrently_not_sequentially()
    test_process_one_chunk_retries_once_then_succeeds()
    test_process_one_chunk_gives_up_after_two_failures()
    print("All tests passed.")
