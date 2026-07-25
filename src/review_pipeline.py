"""Core review pipeline: runs the 14-step framework against a document corpus
via the LLM, chunk by chunk, and aggregates structured findings. Also handles
the iteration pass (prior findings + user row comments + overarching comment).

Known MVP simplification (documented, not silent): the iteration pass re-runs
against the DIGEST only (not every original chunk) plus the prior findings and
user feedback. This is enough to revise/resolve existing findings and apply a
new overarching instruction against the parts of the document the digest
covers, but a request like "also check every appendix table" on iteration 2
may need a fresh full review rather than a cheap iteration. Flagged in the
README as a v2 improvement (full re-chunk on iteration).
"""

import concurrent.futures
import json
import re
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Optional

from src.config import CHUNK_CONCURRENCY, FRAMEWORK_PATH, REVIEW_MAX_TOKENS
from src.llm_client import LLMConfig, call_with_cache
from src.pdf_processing import Page, render_pages_as_text


@dataclass
class Finding:
    finding_id: str
    source_file: str
    page_or_item: str
    section_no: str
    snapshot: str
    discipline: str
    review_comment: str
    status: str
    framework_step: int
    reviewer_comment: str = ""  # filled by user between iterations, not by the LLM
    sl_no: int = 0  # assigned at render time


def load_framework() -> str:
    with open(FRAMEWORK_PATH, "r", encoding="utf-8") as f:
        return f.read()


class TruncatedResponseError(ValueError):
    """Raised when a model response could not yield any usable findings -
    typically because it was cut off (hit max_tokens) before completing even
    one finding object. Distinct from a generic parse error so callers can
    decide whether to treat it as fatal or just "this chunk got nothing"."""


def _extract_json_array(raw_text: str) -> list[dict]:
    """Extract finding objects from the model's response, tolerating a
    response that got cut off mid-generation (hit max_tokens) rather than
    failing the whole chunk. Strategy:

    1. Strip a markdown code fence if present (opening AND/OR closing - a
       truncated response may have the opening fence but never reach a
       closing one).
    2. Try a fast, direct json.loads() first - handles the common
       well-formed case with zero overhead.
    3. If that fails, walk the text looking for top-level `{...}` objects
       and decode each independently via json.JSONDecoder.raw_decode(). Any
       objects that finished generating are kept; a final, incomplete object
       (the one that was mid-flight when the response got cut off) is
       silently dropped rather than poisoning the whole batch.

    Raises TruncatedResponseError only if NOT EVEN ONE complete object could
    be recovered (e.g. cut off during the very first finding).
    """
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        # A single object rather than an array - still usable.
        return [parsed]
    except json.JSONDecodeError:
        pass

    objects: list[dict] = []
    decoder = json.JSONDecoder()
    search_pos = text.find("{")
    while search_pos != -1:
        try:
            obj, end_pos = decoder.raw_decode(text, search_pos)
            objects.append(obj)
            search_pos = text.find("{", end_pos)
        except json.JSONDecodeError:
            # The object starting here is incomplete/malformed - this is
            # expected at the tail of a truncated response. Stop; anything
            # found before this point is still good.
            break

    if not objects:
        raise TruncatedResponseError(
            "Model response ended before any finding finished generating "
            "(likely hit the output token limit on the very first item). "
            f"Raw response (first 1500 chars): {raw_text[:1500]}"
        )
    return objects


def _findings_from_json(items: list[dict]) -> list[Finding]:
    findings = []
    for item in items:
        findings.append(Finding(
            finding_id=str(uuid.uuid4()),
            source_file=item.get("source_file", ""),
            page_or_item=item.get("page_or_item", ""),
            section_no=item.get("section_no", ""),
            snapshot=item.get("snapshot", ""),
            discipline=item.get("discipline", "Geotechnical"),
            review_comment=item.get("review_comment", ""),
            status=item.get("status", "B"),
            framework_step=int(item.get("framework_step", 0) or 0),
        ))
    return findings


def _process_one_chunk(i: int, total: int, chunk: list[Page], config: LLMConfig,
                        framework_text: str, cacheable_context: str,
                        retry_delay_seconds: float = 5.0) -> tuple[list[Finding], Optional[dict], dict]:
    """Runs one chunk's call (+one short retry on failure) and times it.
    Returns (findings, error_or_None, timing_dict). Designed to be safe to
    call from a worker thread - touches no shared mutable state, everything
    it needs is passed in explicitly."""
    chunk_text = render_pages_as_text(chunk)
    dynamic_content = (
        f"DETAILED SECTION TO REVIEW NOW (chunk {i} of {total}):\n{chunk_text}\n\n"
        "Review this chunk against all 14 steps that apply to its content, using the digest "
        "for traceability where needed. Return the JSON findings array now."
    )

    start = time.perf_counter()
    last_exc: Optional[Exception] = None
    raw = None
    for attempt in (1, 2):
        try:
            raw = call_with_cache(config, framework_text, cacheable_context, dynamic_content,
                                   max_tokens=REVIEW_MAX_TOKENS)
            items = _extract_json_array(raw)
            duration = round(time.perf_counter() - start, 1)
            return (_findings_from_json(items), None,
                    {"chunk": i, "total": total, "duration_seconds": duration, "attempts": attempt})
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not sink the job
            last_exc = exc
            if attempt == 1:
                time.sleep(retry_delay_seconds)  # brief pause; likely a transient/rate-limit blip

    duration = round(time.perf_counter() - start, 1)
    error = {
        "chunk": i,
        "total": total,
        "error": f"Chunk {i} of {total} failed after retry: {last_exc}",
        "error_type": type(last_exc).__name__,
        "traceback": traceback.format_exc(),
        "raw_response": raw,  # full text if the last attempt returned one but parsing failed
        "duration_seconds": duration,
    }
    timing = {"chunk": i, "total": total, "duration_seconds": duration, "attempts": 2, "failed": True}
    return [], error, timing


def run_first_pass(config: LLMConfig, digest_pages: list[Page],
                    chunks: list[list[Page]],
                    extra_instructions: Optional[str] = None,
                    progress_cb=None,
                    max_workers: int = CHUNK_CONCURRENCY,
                    cancel_check=None) -> tuple[list[Finding], list[dict], list[dict], bool]:
    """Chunks are processed CONCURRENTLY (bounded worker pool - see
    CHUNK_CONCURRENCY in config.py) rather than one at a time; this is the
    main lever on review latency, since wall-clock is roughly
    (chunk_count / max_workers) * per-call-time instead of
    chunk_count * per-call-time. progress_cb(done, total) fires as each
    chunk completes (may be out of chunk order - that's fine, it's just a
    counter). Findings/errors ARE restored to original chunk/document order
    in the returned lists regardless of completion order, so the final
    output still reads top-to-bottom sensibly.

    Each chunk's call/parse is isolated (with one short retry - see
    _process_one_chunk): a failure on one chunk does not discard findings
    already obtained from OTHER chunks that already cost real API spend.

    cancel_check: optional no-arg callable returning True once cancellation
    is requested (deliberately dependency-injected, not imported from
    job_store here, to avoid a circular import - job_store imports Finding
    from this module). Checked after each chunk completes; if it returns
    True, every NOT-YET-STARTED queued chunk future is cancelled so the job
    stops burning further API spend. Chunks already in flight (at most
    max_workers of them) can't be safely force-killed mid-request - a raw
    Python thread has no safe interrupt - so they finish naturally, bounded
    by LLM_REQUEST_TIMEOUT_SECONDS. Findings from chunks that DID complete
    before cancellation are still returned, not discarded.

    Returns (findings, chunk_errors, chunk_timings, cancelled). chunk_errors
    is a list of dicts with keys chunk, total, error, error_type, traceback,
    and raw_response, empty if everything succeeded - meant for the debug
    log; callers wanting a short on-screen message should read the "error"
    key. chunk_timings has one entry per chunk (success, failure, or
    cancelled) with duration_seconds - meant for the debug log and the
    results-screen timing summary, so a future "why did this take so long"
    question has real data instead of a guess.

    Cost note: framework_text (system prompt) and cacheable_context (digest)
    below are byte-identical across every chunk call - that's exactly what
    prompt caching needs to kick in on repeat calls after the first. Only
    the per-chunk dynamic_content varies.
    """
    framework_text = load_framework()
    if extra_instructions:
        framework_text += (
            "\n\n## PER-JOB EXTRA INSTRUCTIONS (this job only, provided by the user)\n"
            f"{extra_instructions}\n"
        )

    digest_text = render_pages_as_text(digest_pages)
    cacheable_context = (
        "PROJECT DIGEST (opening pages of each source file, for cross-reference / "
        f"traceability checks only - do not re-review this section on its own):\n{digest_text}"
    )
    total = len(chunks)
    results: dict[int, tuple[list[Finding], Optional[dict], dict]] = {}
    completed = 0
    cancelled = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(_process_one_chunk, i, total, chunk, config,
                             framework_text, cacheable_context): i
            for i, chunk in enumerate(chunks, start=1)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            try:
                results[i] = future.result()
            except concurrent.futures.CancelledError:
                results[i] = ([], {
                    "chunk": i, "total": total,
                    "error": f"Chunk {i} of {total} cancelled before it started.",
                    "error_type": "Cancelled", "traceback": "", "raw_response": None,
                }, {"chunk": i, "total": total, "duration_seconds": 0, "attempts": 0, "cancelled": True})
            completed += 1
            if progress_cb:
                progress_cb(completed, total)

            if not cancelled and cancel_check and cancel_check():
                cancelled = True
                for pending_future in future_to_index:
                    pending_future.cancel()  # no-op (returns False) for ones already running

    all_findings: list[Finding] = []
    chunk_errors: list[dict] = []
    chunk_timings: list[dict] = []
    for i in range(1, total + 1):
        findings, error, timing = results[i]
        all_findings.extend(findings)
        if error:
            chunk_errors.append(error)
        chunk_timings.append(timing)

    return all_findings, chunk_errors, chunk_timings, cancelled


def run_iteration(config: LLMConfig, digest_pages: list[Page],
                   prior_findings: list[Finding],
                   overarching_comment: Optional[str] = None,
                   progress_cb=None) -> list[Finding]:
    """Revise prior findings using row-level reviewer_comment fields already
    set on prior_findings, plus an optional overarching free-text comment.
    See module docstring for the digest-only scope of this pass.
    """
    framework_text = load_framework()
    digest_text = render_pages_as_text(digest_pages)
    # Same framework_text + digest shape as run_first_pass, on purpose: if this
    # iteration is requested shortly after the first-pass chunk calls for the
    # same job, it may still land inside the ~5 minute cache window and get a
    # partial cache hit even though this is otherwise a single, one-off call.
    cacheable_context = f"PROJECT DIGEST:\n{digest_text}"

    prior_json = json.dumps([{
        "finding_id": f.finding_id,
        "source_file": f.source_file,
        "page_or_item": f.page_or_item,
        "section_no": f.section_no,
        "snapshot": f.snapshot,
        "discipline": f.discipline,
        "review_comment": f.review_comment,
        "status": f.status,
        "framework_step": f.framework_step,
        "reviewer_comment": f.reviewer_comment,
    } for f in prior_findings], indent=2)

    dynamic_content = (
        "This is a REVISION pass (iteration mode - see instructions above).\n\n"
        f"PRIOR FINDINGS (with any reviewer_comment the user added per row):\n{prior_json}\n\n"
        f"OVERARCHING COMMENT FOR THIS ITERATION: {overarching_comment or '(none provided)'}\n\n"
        "Apply the iteration-mode rules: keep finding_id stable where a finding is unchanged, "
        "revise findings the reviewer commented on, add new findings if the overarching comment "
        "or a row comment reveals something new, and do not silently drop findings. Return the "
        "full updated JSON findings array (all findings, not just changed ones) now. Include "
        "finding_id in each object, reusing the prior one where the finding carries over."
    )

    if progress_cb:
        progress_cb(0, 1)
    raw = call_with_cache(config, framework_text, cacheable_context, dynamic_content,
                           max_tokens=REVIEW_MAX_TOKENS)
    items = _extract_json_array(raw)

    # Preserve finding_id when the model echoes one that matches a prior finding.
    prior_by_id = {f.finding_id: f for f in prior_findings}
    revised: list[Finding] = []
    for item in items:
        fid = item.get("finding_id")
        finding = Finding(
            finding_id=fid if fid in prior_by_id else str(uuid.uuid4()),
            source_file=item.get("source_file", ""),
            page_or_item=item.get("page_or_item", ""),
            section_no=item.get("section_no", ""),
            snapshot=item.get("snapshot", ""),
            discipline=item.get("discipline", "Geotechnical"),
            review_comment=item.get("review_comment", ""),
            status=item.get("status", "B"),
            framework_step=int(item.get("framework_step", 0) or 0),
        )
        revised.append(finding)

    if progress_cb:
        progress_cb(1, 1)
    return revised
