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
from src.review_pipeline import run_first_pass, _process_one_chunk, _is_timeout_class_error
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
        findings, chunk_errors, chunk_timings, cancelled = run_first_pass(
            FAKE_CONFIG, digest, chunks, progress_cb=None, max_workers=3)

    assert chunk_errors == []
    assert cancelled is False
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
        findings, chunk_errors, chunk_timings, cancelled = run_first_pass(
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


def test_process_one_chunk_captures_real_traceback_not_nonetype_none():
    """Regression test for a bug found via two real incident debug logs, both
    showing "traceback": "NoneType: None" instead of anything useful -
    traceback.format_exc() was being called AFTER the code had left the
    except block's scope (once the retry loop over both attempts completed),
    where Python has already cleared the 'currently handled exception'
    context. Fix: capture it INSIDE the except block, while still valid."""
    chunk = [_page("report.pdf", 1, "content")]

    def always_fails(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        raise RuntimeError("a distinctive failure message for this test")

    with patch("src.review_pipeline.call_with_cache", side_effect=always_fails):
        _, error, _ = _process_one_chunk(
            1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0.01)

    assert error["traceback"] != "NoneType: None\n", (
        "traceback capture regressed - this is the exact bug from the incident logs")
    assert "a distinctive failure message for this test" in error["traceback"], (
        f"expected the real traceback to mention the failure, got: {error['traceback']!r}")
    assert "RuntimeError" in error["traceback"]
    print("test_process_one_chunk_captures_real_traceback_not_nonetype_none: OK")


class _FakeAPITimeoutError(Exception):
    """Named to match the real SDKs' exception class name pattern (both
    Anthropic and OpenAI raise a class literally called APITimeoutError) -
    _is_timeout_class_error() checks the TYPE NAME, not the module, so this
    fake is a faithful stand-in without needing either real SDK installed."""


def test_timeout_class_failure_retries_with_reduced_max_tokens():
    """The actual fix for the real incident: retrying an oversized request
    with the IDENTICAL max_tokens is not a retry, it's a second guaranteed
    failure (both real incidents failed identically on both attempts, ~908s
    each). A timeout-class failure should retry with a SMALLER max_tokens,
    giving the retry an actual chance to finish before hitting whatever
    ceiling caused the first failure."""
    chunk = [_page("report.pdf", 1, "content")]
    calls = []

    def fails_once_with_timeout(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        calls.append(max_tokens)
        if len(calls) == 1:
            raise _FakeAPITimeoutError("Request timed out")
        return _finding_json("Recovered with smaller budget")

    with patch("src.review_pipeline.call_with_cache", side_effect=fails_once_with_timeout):
        with patch("src.review_pipeline.REVIEW_MAX_TOKENS", 12000):
            findings, error, timing = _process_one_chunk(
                1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0.01)

    assert error is None, f"expected the reduced-budget retry to succeed, got: {error}"
    assert len(calls) == 2
    assert calls[0] == 12000, "first attempt should use the full configured budget"
    assert calls[1] == 6000, f"retry after a timeout should halve max_tokens, got {calls[1]}"
    print("test_timeout_class_failure_retries_with_reduced_max_tokens: OK")


def test_non_timeout_failure_retries_with_unchanged_max_tokens():
    """A rate-limit or generic transient error has nothing to do with
    response size - retrying with a smaller max_tokens wouldn't help and
    would just produce a worse (truncated) result for no reason. Only
    timeout-class failures should trigger the reduction."""
    chunk = [_page("report.pdf", 1, "content")]
    calls = []

    def fails_once_not_timeout(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        calls.append(max_tokens)
        if len(calls) == 1:
            raise RuntimeError("429 rate limited")
        return _finding_json("Recovered")

    with patch("src.review_pipeline.call_with_cache", side_effect=fails_once_not_timeout):
        with patch("src.review_pipeline.REVIEW_MAX_TOKENS", 12000):
            findings, error, timing = _process_one_chunk(
                1, 1, chunk, FAKE_CONFIG, "framework", "digest", retry_delay_seconds=0.01)

    assert error is None
    assert calls == [12000, 12000], f"non-timeout retry should NOT reduce max_tokens, got {calls}"
    print("test_non_timeout_failure_retries_with_unchanged_max_tokens: OK")


def test_cancellation_stops_not_yet_started_chunks_but_keeps_completed_findings():
    """8 chunks, only 2 workers, each call takes 0.1s. cancel_check flips to
    True after the first chunk completes - by then only ~2 chunks have had a
    chance to start (bounded by max_workers). The rest should never run:
    findings only from chunks that actually completed, and their errors are
    marked "Cancelled", not silently dropped."""
    chunks = [[_page("report.pdf", i, f"content {i}")] for i in range(1, 9)]
    digest = [_page("report.pdf", 0, "digest")]
    calls_made = {"count": 0}

    def fake_call_with_cache(config, system_prompt, cacheable_context, dynamic_content, max_tokens=8192):
        calls_made["count"] += 1
        time.sleep(0.1)
        return _finding_json("Finding")

    cancel_after_first = {"triggered": False}

    def cancel_check():
        # Simulate the user clicking Cancel right after the first chunk lands.
        if cancel_after_first["triggered"]:
            return True
        return False

    with patch("src.review_pipeline.call_with_cache", side_effect=fake_call_with_cache):
        def progress_cb(done, total):
            if done == 1:
                cancel_after_first["triggered"] = True

        findings, chunk_errors, chunk_timings, cancelled = run_first_pass(
            FAKE_CONFIG, digest, chunks, progress_cb=progress_cb, max_workers=2,
            cancel_check=cancel_check)

    assert cancelled is True
    # With only 2 workers, at most a small handful of chunks could have started
    # before cancellation kicked in - nowhere near all 8.
    assert calls_made["count"] < 8, (
        f"expected cancellation to prevent most of the 8 chunks from ever starting, "
        f"but {calls_made['count']} calls were made")
    assert len(findings) == calls_made["count"], "findings should exist only for chunks that actually ran"
    cancelled_entries = [e for e in chunk_errors if e["error_type"] == "Cancelled"]
    assert len(cancelled_entries) > 0, "expected at least one chunk to be recorded as cancelled, not silently dropped"
    print(f"test_cancellation_stops_not_yet_started_chunks_but_keeps_completed_findings: OK "
          f"({calls_made['count']}/8 chunks ran before cancel)")


def test_is_timeout_class_error_matches_by_type_name():
    assert _is_timeout_class_error(_FakeAPITimeoutError("x")) is True
    assert _is_timeout_class_error(RuntimeError("429 rate limited")) is False
    assert _is_timeout_class_error(ValueError("bad json")) is False
    print("test_is_timeout_class_error_matches_by_type_name: OK")


if __name__ == "__main__":
    test_run_first_pass_preserves_chunk_order_despite_reversed_completion()
    test_run_first_pass_runs_concurrently_not_sequentially()
    test_process_one_chunk_retries_once_then_succeeds()
    test_process_one_chunk_gives_up_after_two_failures()
    test_process_one_chunk_captures_real_traceback_not_nonetype_none()
    test_timeout_class_failure_retries_with_reduced_max_tokens()
    test_non_timeout_failure_retries_with_unchanged_max_tokens()
    test_is_timeout_class_error_matches_by_type_name()
    test_cancellation_stops_not_yet_started_chunks_but_keeps_completed_findings()
    print("All tests passed.")
