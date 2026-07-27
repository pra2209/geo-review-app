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

from src.config import (
    ADAPTIVE_SPLIT_MAX_DEPTH,
    ADAPTIVE_SPLIT_MAX_REQUESTS_PER_CHUNK,
    CHUNK_CONCURRENCY,
    FRAMEWORK_PATH,
    REVIEW_MAX_TOKENS,
    REVIEW_RETRY_MIN_TOKENS,
)
from src.llm_client import LLMConfig, call_with_cache
from src.pdf_processing import (
    MIN_PAGE_FRAGMENT_CHARS,
    Page,
    describe_pages,
    render_pages_as_text,
    split_page,
)


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
    """No complete finding object could be recovered from a model response."""

    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


class PartialResponseError(ValueError):
    """The response contained complete findings but ended malformed/truncated.

    Keeping the recovered items on the exception lets the adaptive retry path
    fall back to them if a smaller split request also fails, without silently
    presenting an incomplete response as fully successful.
    """

    def __init__(self, message: str, items: list[dict], raw_text: str):
        super().__init__(message)
        self.items = items
        self.raw_text = raw_text


def _extract_json_array_details(raw_text: str) -> tuple[list[dict], bool]:
    """Return ``(items, salvaged)`` from a model response.

    ``salvaged`` is true when complete objects were recovered from otherwise
    invalid JSON. The public compatibility wrapper below still returns only
    the items, while the review worker uses this signal to adaptively split
    and retry instead of silently accepting an incomplete generation.
    """
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed, False
        if isinstance(parsed, dict):
            return [parsed], False
        raise ValueError("Model response JSON must be an object or array of objects")
    except json.JSONDecodeError:
        pass

    objects: list[dict] = []
    decoder = json.JSONDecoder()
    search_pos = text.find("{")
    while search_pos != -1:
        try:
            obj, end_pos = decoder.raw_decode(text, search_pos)
            if isinstance(obj, dict):
                objects.append(obj)
            search_pos = text.find("{", end_pos)
        except json.JSONDecodeError:
            break

    if not objects:
        raise TruncatedResponseError(
            "Model response ended before any finding finished generating "
            "(likely hit the output token limit or a request-duration limit on the first item). "
            f"Raw response (first 1500 chars): {raw_text[:1500]}",
            raw_text=raw_text,
        )
    return objects, True


def _extract_json_array(raw_text: str) -> list[dict]:
    """Compatibility wrapper used by existing tests and iteration mode."""
    items, _ = _extract_json_array_details(raw_text)
    return items


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



def _decode_partial_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except (json.JSONDecodeError, TypeError):
        return value.replace('\\"', '"').replace('\\n', ' ')


def _extract_incomplete_finding_hint(raw_text: Optional[str]) -> Optional[dict]:
    """Recover locator fields from an unfinished first finding, when available.

    This is diagnostic metadata only. It must never be treated as a validated
    finding because the model did not finish the JSON object.
    """
    if not raw_text:
        return None
    hint: dict[str, str] = {}
    for field in ("source_file", "page_or_item", "section_no", "snapshot"):
        match = re.search(rf'"{field}"\s*:\s*"((?:\\.|[^"\\])*)"', raw_text)
        if match:
            hint[field] = _decode_partial_json_string(match.group(1)).strip()
    return hint or None


def _is_no_text_output_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "notext" in name
        or "outputexhausted" in name
        or "no text content block" in message
        or "only thinking content block" in message
        or "returned thinking content block" in message
    )

def _is_timeout_class_error(exc: Exception) -> bool:
    """Provider-neutral timeout detection for Anthropic/OpenAI/Gemini SDKs."""
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "timeout" in name
        or "timed out" in message
        or "request timeout" in message
        or "request was interrupted" in message
        or "request timed out or interrupted" in message
    )


def _is_request_size_error(exc: Exception) -> bool:
    """Detect context/request-size failures whose correct recovery is splitting."""
    message = str(exc).lower()
    indicators = (
        "context length",
        "context_length",
        "maximum context",
        "too many tokens",
        "request too large",
        "payload too large",
        "maximum request size",
        "413",
    )
    return any(indicator in message for indicator in indicators)


def _should_split_after_failure(exc: Exception) -> bool:
    return (
        isinstance(exc, (TruncatedResponseError, PartialResponseError))
        or _is_timeout_class_error(exc)
        or _is_request_size_error(exc)
        or _is_no_text_output_error(exc)
    )


def _preserve_output_budget_after_split(exc: Exception) -> bool:
    """Output exhaustion needs smaller input but not a smaller response budget."""
    return (
        isinstance(exc, (TruncatedResponseError, PartialResponseError))
        or _is_no_text_output_error(exc)
    )


def _split_chunk_for_retry(chunk: list[Page]) -> Optional[tuple[list[Page], list[Page]]]:
    """Bisect a failed chunk by rendered size, splitting one dense page if needed."""
    if len(chunk) > 1:
        weights = [len(page.text) + 180 for page in chunk]
        target = sum(weights) / 2
        running = 0
        split_at = 1
        for index, weight in enumerate(weights[:-1], start=1):
            running += weight
            split_at = index
            if running >= target:
                break
        return chunk[:split_at], chunk[split_at:]

    if not chunk:
        return None
    page = chunk[0]
    if len(page.text) < MIN_PAGE_FRAGMENT_CHARS * 2:
        return None
    fragments = split_page(page, max(MIN_PAGE_FRAGMENT_CHARS, len(page.text) // 2))
    if len(fragments) < 2:
        return None
    midpoint = max(1, len(fragments) // 2)
    return fragments[:midpoint], fragments[midpoint:]


def _dynamic_content(label: str, chunk: list[Page]) -> str:
    chunk_text = render_pages_as_text(chunk)
    return (
        f"DETAILED SECTION TO REVIEW NOW ({label}):\n{chunk_text}\n\n"
        "Review only this section against all 14 steps that apply to its content, "
        "using the digest only for cross-reference and traceability. Return the JSON "
        "findings array immediately, with no prose or markdown fences. Keep each finding "
        "concise and prioritize finishing the complete array over elaborating any one item."
    )


def _process_one_chunk(i: int, total: int, chunk: list[Page], config: LLMConfig,
                       framework_text: str, cacheable_context: str,
                       retry_delay_seconds: float = 5.0) -> tuple[list[Finding], Optional[dict], dict]:
    """Review one top-level chunk with bounded recursive recovery.

    Recovery policy:
    * timeout, request-size and truncated/partial-output failures split the
      input into smaller children rather than repeating the same doomed call;
    * a child may split again, but depth and total request count are capped;
    * an unsplittable leaf gets one retry; timeout retries use a smaller
      generation ceiling, while thinking-only/truncated-output retries preserve
      the JSON budget and switch Anthropic to direct-output mode;
    * successful child findings are retained even if a sibling ultimately
      fails, and the caller receives an explicit partial-result warning.
    """
    start = time.perf_counter()
    request_count = 0
    adaptive_splits = 0
    last_raw: Optional[str] = None
    last_traceback = ""
    failure_messages: list[str] = []
    failure_types: list[str] = []
    failed_parts: list[dict] = []
    response_hints: list[dict] = []
    parent_salvage_used = False

    def invoke(part: list[Page], label: str, max_tokens: int,
               response_mode: str = "balanced") -> list[Finding]:
        nonlocal request_count, last_raw
        if request_count >= ADAPTIVE_SPLIT_MAX_REQUESTS_PER_CHUNK:
            raise RuntimeError(
                "Adaptive retry request limit reached for this chunk "
                f"({ADAPTIVE_SPLIT_MAX_REQUESTS_PER_CHUNK} provider calls)."
            )
        request_count += 1
        last_raw = None
        last_raw = call_with_cache(
            config,
            framework_text,
            cacheable_context,
            _dynamic_content(label, part),
            max_tokens=max_tokens,
            response_mode=response_mode,
        )
        items, salvaged = _extract_json_array_details(last_raw)
        if salvaged:
            raise PartialResponseError(
                f"{label} returned incomplete JSON; complete objects were salvaged",
                items,
                last_raw,
            )
        return _findings_from_json(items)

    def review_part(
        part: list[Page],
        label: str,
        depth: int,
        max_tokens: int,
    ) -> tuple[list[Finding], bool]:
        """Return (findings, complete). ``complete`` is false if any leaf failed."""
        nonlocal adaptive_splits, last_traceback, last_raw, parent_salvage_used

        def capture_failure(exc: Exception) -> tuple[list[Finding], Optional[dict]]:
            """Capture recoverable output and a locator from one failed request."""
            nonlocal last_raw
            if isinstance(exc, PartialResponseError):
                last_raw = exc.raw_text
                recovered = _findings_from_json(exc.items)
            elif isinstance(exc, TruncatedResponseError):
                last_raw = exc.raw_text
                recovered = []
            else:
                raw = getattr(exc, "raw_text", None)
                if raw is not None:
                    last_raw = raw
                recovered = []
            locator = _extract_incomplete_finding_hint(last_raw)
            if locator and locator not in response_hints:
                response_hints.append(locator)
            return recovered, locator

        try:
            return invoke(part, label, max_tokens, response_mode="balanced"), True
        except Exception as first_exc:  # noqa: BLE001 - isolate one failed part
            last_traceback = traceback.format_exc()
            salvaged, hint = capture_failure(first_exc)
            effective_exc = first_exc
            direct_retry_attempted = False

            # A thinking-only response is not evidence that the input is too
            # large. Retry the same bounded page range once with thinking off
            # before multiplying requests through another split. This directly
            # addresses Sonnet 5 consuming the shared output budget on thinking.
            if _is_no_text_output_error(first_exc):
                direct_retry_attempted = True
                if retry_delay_seconds:
                    time.sleep(retry_delay_seconds)
                try:
                    return invoke(
                        part, label, max_tokens, response_mode="direct",
                    ), True
                except Exception as direct_exc:  # noqa: BLE001
                    last_traceback = traceback.format_exc()
                    direct_salvaged, direct_hint = capture_failure(direct_exc)
                    if direct_salvaged:
                        salvaged = direct_salvaged
                    if direct_hint:
                        hint = direct_hint
                    effective_exc = direct_exc

            can_split = (
                _should_split_after_failure(effective_exc)
                and depth < ADAPTIVE_SPLIT_MAX_DEPTH
                and request_count < ADAPTIVE_SPLIT_MAX_REQUESTS_PER_CHUNK
            )
            split = _split_chunk_for_retry(part) if can_split else None
            if split is not None:
                adaptive_splits += 1
                child_tokens = (
                    max_tokens
                    if _preserve_output_budget_after_split(effective_exc)
                    else max(REVIEW_RETRY_MIN_TOKENS, max_tokens // 2)
                )
                findings: list[Finding] = []
                all_complete = True
                for child_no, child in enumerate(split, start=1):
                    child_findings, child_complete = review_part(
                        child,
                        f"{label}.{child_no}",
                        depth + 1,
                        child_tokens,
                    )
                    findings.extend(child_findings)
                    all_complete = all_complete and child_complete

                # Prefer the smaller child calls to avoid duplicating the
                # salvaged parent objects. Parent salvage is only a last resort
                # when neither child yielded anything usable.
                if not findings and salvaged:
                    findings = salvaged
                    parent_salvage_used = True
                return findings, all_complete

            # Thinking-only failures have already received their one direct
            # retry above. Do not issue a third identical leaf request.
            if direct_retry_attempted:
                second_exc = effective_exc
            else:
                if retry_delay_seconds:
                    time.sleep(retry_delay_seconds)
                retry_tokens = (
                    max_tokens
                    if _preserve_output_budget_after_split(effective_exc)
                    else max(REVIEW_RETRY_MIN_TOKENS, max_tokens // 2)
                    if _should_split_after_failure(effective_exc)
                    else max_tokens
                )
                try:
                    retry_mode = (
                        "direct" if _preserve_output_budget_after_split(effective_exc)
                        else "balanced"
                    )
                    return invoke(
                        part, label, retry_tokens, response_mode=retry_mode,
                    ), True
                except Exception as retry_exc:  # noqa: BLE001
                    last_traceback = traceback.format_exc()
                    retry_salvaged, retry_hint = capture_failure(retry_exc)
                    if retry_salvaged:
                        salvaged = retry_salvaged
                    if retry_hint:
                        hint = retry_hint
                    second_exc = retry_exc

            failure_types.append(type(second_exc).__name__)
            failure_messages.append(f"{label} failed after retry/recovery: {second_exc}")
            failed_parts.append({
                "label": label,
                **describe_pages(part),
                "error_type": type(second_exc).__name__,
                "error": str(second_exc),
                "partial_findings_kept": len(salvaged),
                "incomplete_finding_hint": hint,
            })
            return salvaged, False

    label = f"chunk {i} of {total}"
    findings, complete = review_part(chunk, label, depth=0, max_tokens=REVIEW_MAX_TOKENS)
    duration = round(time.perf_counter() - start, 1)
    coverage = describe_pages(chunk)
    timing = {
        "chunk": i,
        "total": total,
        **coverage,
        "duration_seconds": duration,
        "attempts": request_count,
        "adaptive_splits": adaptive_splits,
        "input_chars": len(render_pages_as_text(chunk)),
        "failed": not bool(findings) and not complete,
        "partial": bool(findings) and not complete,
    }

    if complete:
        return findings, None, timing

    error_type = failure_types[-1] if failure_types else "AdaptiveSplitPartialFailure"
    error = {
        "chunk": i,
        "total": total,
        "error": (
            f"Chunk {i} of {total} completed only partially after bounded adaptive recovery: "
            + " | ".join(failure_messages or ["one or more split parts failed"])
        ),
        "error_type": error_type,
        "traceback": last_traceback,
        "raw_response": last_raw,
        "duration_seconds": duration,
        "partial_findings_kept": len(findings),
        "adaptive_splits": adaptive_splits,
        "request_count": request_count,
        "coverage": coverage,
        "failed_parts": failed_parts,
        "response_hints": response_hints,
        "parent_salvage_used": parent_salvage_used,
    }
    return findings, error, timing


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
