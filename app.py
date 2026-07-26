"""Geotechnical Report Review — Streamlit MVP.

Single-file app on purpose for v1 (see README for the v2 refactor note if this
grows). Screens, in order: password gate -> upload -> summary + framework
preview + extra instructions -> review progress -> results + iterate loop ->
finished.
"""

import threading
import time
import traceback

import streamlit as st

from src.config import ALLOWED_MODELS, CHUNK_CONCURRENCY, DEFAULT_MODEL, MAX_FILES_PER_JOB
from src import job_store
from src.excel_io import write_excel, read_reviewer_comments, DISCLAIMER
from src.llm_client import LLMConfig, validate_key
from src.pdf_processing import (
    MIN_MEANINGFUL_CHARS_PER_PAGE,
    build_digest,
    chunk_pages,
    compute_text_coverage,
    extract_all,
    get_and_clear_extraction_warnings,
    render_pages_as_text,
)
from src.review_pipeline import load_framework, run_first_pass, run_iteration, Finding
from src.summary import generate_summary

st.set_page_config(page_title="Geotechnical Report Review", layout="wide")

STATUS_LABELS = {
    "D": "Rejected (critical)", "C": "Revise & Resubmit", "B": "Incorporate & Proceed",
    "A": "Proceed", "E": "Not Required",
}


def _get_secret(key: str, default=None):
    """st.secrets.get(...) raises StreamlitSecretNotFoundError - not just
    returning the default - when no secrets.toml file exists at all (as
    opposed to existing but not containing this key, which .get() handles
    fine). That distinction matters here: this app fails CLOSED with no
    ACCESS_CODE configured, and a raw uncaught exception showing a full
    traceback (including local filesystem paths) on a fresh checkout is a
    worse failure mode than the friendly "not configured yet" message the
    fail-closed path is supposed to show instead."""
    try:
        return st.secrets.get(key, default)
    except Exception:  # noqa: BLE001 - secrets subsystem unavailable = key not set, not a crash
        return default


def _log_error(job_id: str, stage: str, exc: Exception, config: LLMConfig | None = None,
                extra: dict | None = None):
    """Captures full diagnostic context for one error into the job's debug
    log - never the API key, never PDF binary content. Intended to be handed
    straight to Claude Code for debugging instead of copy-pasting a truncated
    on-screen message.

    MUST NEVER RAISE. This runs inside background-thread except blocks,
    immediately before the job_store.set_status(..., "error", ...) call that
    is the ONLY thing that lets the UI ever learn the job died - see the
    incident this comment is a postmortem of: a background thread held a
    stale reference to job_store (captured before a hot-reload added
    append_debug_event), so THIS call raised AttributeError, which propagated
    out uncaught and skipped the status update entirely. The job was then
    stuck at status="reviewing" forever - not slow, not hung, just never
    informed anyone it had died. Streamlit's "Stop" button cannot help with
    that; there was nothing left running to stop.
    """
    try:
        event = {
            "stage": stage,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
        }
        if config is not None:
            event["provider"] = config.provider
            event["model"] = config.model
        if extra:
            event.update(extra)
        job_store.append_debug_event(job_id, event)
    except Exception:  # noqa: BLE001 - logging the error must never prevent reporting it
        pass


def _log_extraction_warnings(job_id: str, stage: str):
    """Call right after extract_all(). PyMuPDF's native per-occurrence xref
    warnings are silenced (see pdf_processing.py) - this captures a one-line
    summary instead, so a genuinely corrupted source PDF is still visible in
    the debug log without thousands of raw lines cluttering it."""
    try:
        warnings = get_and_clear_extraction_warnings()
        if warnings:
            job_store.append_debug_event(job_id, {
                "stage": f"{stage}_pdf_extraction_warnings",
                "warning_count": len(warnings),
                "sample": warnings[:5],
                "note": "Recoverable PDF structural issues (e.g. malformed xref entries) - "
                        "extraction likely still succeeded via fallback parsing, but the "
                        "source PDF may be worth re-exporting from its original source.",
            })
    except Exception:  # noqa: BLE001 - never let this block the actual pipeline
        pass


def _debug_log_download_button(job_id: str, key: str):
    if job_store.has_debug_log(job_id):
        st.download_button(
            "Download error log (for debugging)",
            data=job_store.get_debug_log_bytes(job_id),
            file_name=f"debug_log_{job_id[:8]}.json",
            mime="application/json",
            key=key,
            help="Full diagnostic detail - stage, traceback, raw model responses where "
                 "available. Never includes your API key or the PDF's binary content - but "
                 "raw model responses can quote/paraphrase your report's content. Review "
                 "before sharing outside your organization; safe to hand to Claude Code "
                 "within this same session.",
        )


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------

def render_password_gate() -> bool:
    if st.session_state.get("authenticated"):
        return True
    st.title("Geotechnical Report Review")

    expected = _get_secret("ACCESS_CODE")
    if not expected:
        # Fails CLOSED, not open. This used to fall back to a hardcoded
        # credential when Secrets wasn't configured yet - that made sense as
        # a one-time bootstrap, but leaving a real credential checked into a
        # public repo indefinitely is a genuine, unnecessary risk now that
        # Secrets is demonstrably working (this deploy already has
        # SHARED_PROVIDER etc. configured there). No ACCESS_CODE means no
        # one gets in until an administrator sets one, full stop - not "use
        # this password everyone can read on GitHub."
        st.error("This app isn't configured yet: no ACCESS_CODE is set in Secrets. "
                  "An administrator needs to set one before anyone can sign in.")
        return False

    code = st.text_input("Access code", type="password")
    if st.button("Enter"):
        if code == expected:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect access code.")
    return False


# ---------------------------------------------------------------------------
# LLM key/model picker (sidebar, session-only - never written to disk)
# ---------------------------------------------------------------------------

def render_llm_sidebar() -> LLMConfig | None:
    st.sidebar.header("LLM settings")

    # Optional shared team key: if the deployer configured SHARED_PROVIDER /
    # SHARED_MODEL / SHARED_{PROVIDER}_API_KEY in Secrets, teammates get a
    # working config with zero setup - no personal API account needed. Pair
    # this with a spend cap on that key in the provider's console; that cap,
    # not which UI issues the calls, is what actually bounds cost. Falls back
    # to today's BYOK-only flow untouched if none of this is configured.
    shared_provider = _get_secret("SHARED_PROVIDER")
    shared_model = _get_secret("SHARED_MODEL")
    shared_key = None
    if shared_provider == "anthropic":
        shared_key = _get_secret("SHARED_ANTHROPIC_API_KEY")
    elif shared_provider == "openai":
        shared_key = _get_secret("SHARED_OPENAI_API_KEY")
    elif shared_provider == "gemini":
        shared_key = _get_secret("SHARED_GEMINI_API_KEY")
    shared_available = bool(shared_provider and shared_model and shared_key)

    if shared_available:
        use_own_key = st.sidebar.checkbox(
            "Use your own API key instead of the shared team key",
            value=st.session_state.get("use_own_key", False))
        st.session_state["use_own_key"] = use_own_key
        if not use_own_key:
            st.sidebar.success(f"Using shared team key ({shared_provider}, {shared_model}).")
            return LLMConfig(provider=shared_provider, model=shared_model, api_key=shared_key)

    provider = st.sidebar.selectbox("Provider", ["anthropic", "openai", "gemini"])
    model = st.sidebar.selectbox("Model", ALLOWED_MODELS[provider],
                                  index=ALLOWED_MODELS[provider].index(DEFAULT_MODEL[provider]))
    api_key = st.sidebar.text_input("API key (yours - not stored)", type="password",
                                     value=st.session_state.get("api_key", ""))
    st.sidebar.caption(
        "Only reasoning-capable models are listed - small/fast models are excluded because "
        "this review requires multi-step standards reasoning. Gemini keys come from Google AI "
        "Studio (aistudio.google.com/apikey) - the free tier works but uses your content to "
        "improve Google's products; paid tier turns that off, same as the other providers."
    )
    if st.sidebar.button("Validate key"):
        if not api_key:
            st.sidebar.error("Enter a key first.")
        else:
            ok, msg = validate_key(LLMConfig(provider, model, api_key))
            if ok:
                st.sidebar.success("Key works.")
            else:
                st.sidebar.error(f"Key/model check failed: {msg}")

    st.session_state["api_key"] = api_key
    if not api_key:
        return None
    return LLMConfig(provider=provider, model=model, api_key=api_key)


# ---------------------------------------------------------------------------
# Screen: upload
# ---------------------------------------------------------------------------

def render_upload_screen(config: LLMConfig | None):
    st.header("1. Upload the project's PDF report(s)")
    st.caption(
        f"Up to {MAX_FILES_PER_JOB} files. Multiple files are treated as ONE project's material "
        "(e.g. a GDR plus a referenced GIR), reviewed together."
    )
    uploaded = st.file_uploader("PDF files", type=["pdf"], accept_multiple_files=True)

    if uploaded and len(uploaded) > MAX_FILES_PER_JOB:
        st.error(f"Please upload at most {MAX_FILES_PER_JOB} files.")
        return

    disabled = not uploaded or config is None
    if config is None:
        st.info("Enter and validate an API key in the sidebar to continue.")

    if st.button("Summarize", disabled=disabled):
        files = [(f.name, f.read()) for f in uploaded]
        job_id = job_store.create_job()
        job_store.save_uploaded_files(job_id, files)
        st.session_state["job_id"] = job_id
        st.query_params["job_id"] = job_id
        _start_summarization_bg(job_id, config)
        st.rerun()


def _start_summarization_bg(job_id: str, config: LLMConfig):
    def _run():
        try:
            job_store.set_status(job_id, "summarizing", stage="Extracting text and summarizing...",
                                  current=0, total=1)
            job_store.set_stage_start(job_id)
            files = job_store.get_uploaded_files(job_id)
            pages = extract_all(files)
            _log_extraction_warnings(job_id, "summarizing")
            job_store.save_text_coverage(job_id, compute_text_coverage(pages))
            digest = build_digest(pages)
            summary = generate_summary(config, digest)
            job_store.save_summary(job_id, summary)
            job_store.set_status(job_id, "summarized", stage="Summary ready", current=1, total=1)
        except Exception as exc:  # noqa: BLE001
            _log_error(job_id, "summarizing", exc, config)
            job_store.set_status(job_id, "error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Screen: summary + framework preview + extra instructions + start review
# ---------------------------------------------------------------------------

def render_summary_screen(job_id: str, config: LLMConfig | None):
    st.header("2. Project summary")
    summary = job_store.get_summary(job_id)
    st.markdown(summary)

    coverage = job_store.get_text_coverage(job_id)
    if coverage and coverage["total_pages"] and coverage["low_text_fraction"] > 0.15:
        st.warning(
            f"⚠️ {coverage['low_text_pages']} of {coverage['total_pages']} pages "
            f"({coverage['low_text_fraction']:.0%}) extracted to almost no text "
            f"(<{MIN_MEANINGFUL_CHARS_PER_PAGE} characters) - likely scanned images, "
            "photos, or drawings with no selectable text. This app does not run OCR, so "
            "**those pages will not be meaningfully reviewed**, even though they'll still "
            "be counted as processed. Sample: " + ", ".join(coverage["low_text_page_refs"])
        )

    st.header("3. Review logic that will be applied")
    with st.expander("Show the 14-step review framework", expanded=False):
        st.markdown(load_framework())

    st.header("4. Add extra review points (optional)")
    st.caption(
        "These apply to THIS review only - they are not saved back into the framework and "
        "won't affect future reviews."
    )
    extra = st.text_area("Extra instructions for this job", value="", height=100)

    disabled = config is None
    if st.button("Start Review", disabled=disabled):
        job_store.save_extra_instructions(job_id, extra)
        _start_review_bg(job_id, config, extra, iteration=1)
        st.rerun()


def _start_review_bg(job_id: str, config: LLMConfig, extra_instructions: str, iteration: int):
    def _run():
        try:
            job_store.clear_cancel(job_id)
            job_store.set_status(job_id, "reviewing", stage="Starting review...", current=0, total=1)
            job_store.set_stage_start(job_id)
            run_start = time.time()
            files = job_store.get_uploaded_files(job_id)
            pages = extract_all(files)
            _log_extraction_warnings(job_id, "reviewing")
            digest = build_digest(pages)
            chunks = chunk_pages(pages)
            # Record the actual bounded request plan. This contains no API key
            # or PDF binary, but filenames are retained for source traceability.
            job_store.append_debug_event(job_id, {
                "stage": "review_plan",
                "source_files": list(dict.fromkeys(page.source_file for page in pages)),
                "extracted_pages": len(pages),
                "digest_fragments": len(digest),
                "digest_chars": len(render_pages_as_text(digest)),
                "chunk_count": len(chunks),
                "chunk_chars": [len(render_pages_as_text(chunk)) for chunk in chunks],
                "chunk_fragments": [len(chunk) for chunk in chunks],
            })

            def progress_cb(done, total):
                job_store.set_status(job_id, "reviewing",
                                      stage=f"Reviewing section {done} of {total}...",
                                      current=done, total=total)

            findings, chunk_errors, chunk_timings, cancelled = run_first_pass(
                config, digest, chunks, extra_instructions or None, progress_cb=progress_cb,
                cancel_check=lambda: job_store.is_cancel_requested(job_id))
            total_duration = round(time.time() - run_start, 1)

            for ce in chunk_errors:
                job_store.append_debug_event(job_id, {"stage": "reviewing_chunk", **ce,
                                                        "provider": config.provider, "model": config.model})
            for ct in chunk_timings:
                job_store.append_debug_event(job_id, {"stage": "chunk_timing", **ct})

            job_store.save_run_stats(job_id, iteration, {
                "total_duration_seconds": total_duration,
                "chunk_count": len(chunks),
                "workers": CHUNK_CONCURRENCY,
                "chunks_failed": len(chunk_errors),
                "chunk_durations_seconds": [t["duration_seconds"] for t in chunk_timings],
                "llm_request_count": sum(t.get("attempts", 0) for t in chunk_timings),
                "adaptive_splits": sum(t.get("adaptive_splits", 0) for t in chunk_timings),
                "cancelled": cancelled,
            })

            if not findings and (chunk_errors or cancelled):
                status = "cancelled" if cancelled else "error"
                job_store.set_status(
                    job_id, status,
                    error="All chunks failed:\n" + "\n".join(ce["error"] for ce in chunk_errors))
                return

            job_store.save_findings(job_id, iteration, findings)
            job_store.save_warnings(job_id, iteration, [ce["error"] for ce in chunk_errors])
            excel_bytes = write_excel(findings, job_id)
            job_store.save_excel(job_id, iteration, excel_bytes)
            job_store.set_iteration(job_id, iteration)
            if cancelled:
                stage = f"Cancelled - {len(findings)} finding(s) from completed sections kept"
                job_store.set_status(job_id, "cancelled", stage=stage, current=1, total=1)
            else:
                stage = "Review complete" if not chunk_errors else (
                    f"Review complete with {len(chunk_errors)} section(s) needing a retry - see warnings below")
                job_store.set_status(job_id, "review_done", stage=stage, current=1, total=1)
        except Exception as exc:  # noqa: BLE001
            _log_error(job_id, "reviewing", exc, config)
            job_store.set_status(job_id, "error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()


def _start_iteration_bg(job_id: str, config: LLMConfig, overarching_comment: str,
                         reviewer_comments: dict[str, str], next_iteration: int):
    def _run():
        try:
            job_store.set_status(job_id, "iterating", stage="Starting revision...", current=0, total=1)
            job_store.set_stage_start(job_id)
            prior = job_store.get_findings(job_id, next_iteration - 1) or []
            for f in prior:
                if f.finding_id in reviewer_comments:
                    f.reviewer_comment = reviewer_comments[f.finding_id]

            files = job_store.get_uploaded_files(job_id)
            pages = extract_all(files)
            _log_extraction_warnings(job_id, "iterating")
            digest = build_digest(pages)

            def progress_cb(done, total):
                job_store.set_status(job_id, "iterating", stage="Applying your feedback...",
                                      current=done, total=total)

            revised = run_iteration(config, digest, prior, overarching_comment or None,
                                     progress_cb=progress_cb)
            job_store.save_findings(job_id, next_iteration, revised)
            excel_bytes = write_excel(revised, job_id)
            job_store.save_excel(job_id, next_iteration, excel_bytes)
            job_store.set_iteration(job_id, next_iteration)
            job_store.set_status(job_id, "review_done", stage="Revision complete", current=1, total=1)
        except Exception as exc:  # noqa: BLE001
            # Don't strand the user on a dead-end error screen: the PRIOR
            # iteration's findings/Excel are untouched on disk (we never
            # called set_iteration for this failed attempt), so fall back to
            # showing that instead of a fatal job-wide error.
            _log_error(job_id, "iterating", exc, config)
            job_store.save_iteration_error(job_id, str(exc))
            job_store.set_status(job_id, "review_done",
                                  stage="Revision attempt failed - showing your last good result",
                                  current=1, total=1)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Screen: progress (polling)
# ---------------------------------------------------------------------------

def render_progress_screen(job_id: str, status: dict):
    st.header("Working...")
    total = max(status["progress_total"], 1)
    current = status["progress_current"]
    st.progress(current / total, text=status["progress_stage"] or "Working...")

    stage_start = job_store.get_stage_start(job_id)
    if stage_start:
        elapsed = int(time.time() - stage_start)
        st.caption(f"Elapsed: {elapsed // 60}m {elapsed % 60}s — this page auto-refreshes "
                   "every couple of seconds.")
    else:
        st.caption("This page auto-refreshes every couple of seconds.")

    st.divider()
    st.caption(
        "Note: this app's background work runs as a server-side thread, not tied to your "
        "browser tab - Streamlit's own Stop button can't reach it. Use the controls below "
        "instead if you want to stop waiting."
    )
    col1, col2 = st.columns(2)
    with col1:
        if status["status"] == "reviewing" and st.button("Cancel this review", key=f"cancel_{job_id}"):
            job_store.request_cancel(job_id)
            st.info("Cancelling - sections already in progress will finish, but no new ones "
                    "will start. This page will update shortly.")
            time.sleep(2)
            st.rerun()
    with col2:
        if st.button("Abandon and start a new job now", key=f"abandon_{job_id}"):
            job_store.request_cancel(job_id)  # best-effort - stops queued-but-not-started work
            st.session_state.pop("job_id", None)
            st.query_params.clear()
            st.rerun()

    time.sleep(2)
    st.rerun()


# ---------------------------------------------------------------------------
# Screen: results + iterate loop
# ---------------------------------------------------------------------------

def render_results_screen(job_id: str, config: LLMConfig | None, iteration: int, finished: bool):
    findings = job_store.get_findings(job_id, iteration) or []
    excel_bytes = job_store.get_excel(job_id, iteration)
    warnings = job_store.get_warnings(job_id, iteration)
    iteration_error = job_store.pop_iteration_error(job_id)

    if iteration_error:
        st.error(f"Your last revision attempt failed, so this is still your last successful "
                  f"result (iteration {iteration}) - nothing was lost. Error was: {iteration_error}")
        _debug_log_download_button(job_id, key="iteration_error_debug_log")

    st.header(f"5. Results (iteration {iteration})")
    run_stats = job_store.get_run_stats(job_id, iteration)
    if run_stats:
        total_s = run_stats["total_duration_seconds"]
        durations = run_stats.get("chunk_durations_seconds") or []
        slowest = f", slowest chunk {max(durations):.0f}s" if durations else ""
        st.caption(
            f"Completed in {int(total_s // 60)}m {int(total_s % 60)}s across "
            f"{run_stats['chunk_count']} section(s) ({run_stats['workers']} concurrent, "
            f"{run_stats['chunks_failed']} needed a retry{slowest})."
        )
    if warnings:
        with st.expander(f"⚠️ {len(warnings)} section(s) had a problem and were skipped "
                          "(findings below are still complete for everything that succeeded)",
                          expanded=False):
            for w in warnings:
                st.warning(w)
            st.caption(
                "This usually means the model's response for that section got cut off. "
                "Re-running the review (or a Revise Review iteration once the rest looks good) "
                "will retry that content."
            )
            _debug_log_download_button(job_id, key="warnings_debug_log")
    counts = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    cols = st.columns(5)
    for col, code in zip(cols, ["D", "C", "B", "A", "E"]):
        col.metric(STATUS_LABELS[code], counts.get(code, 0))

    st.caption(DISCLAIMER)
    st.download_button("Download Excel", data=excel_bytes,
                        file_name=f"geotech_review_{job_id[:8]}_v{iteration}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with st.expander("View findings", expanded=False):
        for f in findings:
            st.markdown(f"**[{f.status}] {f.snapshot}** — {f.source_file}, {f.page_or_item}")
            st.write(f.review_comment)
            st.divider()

    if finished:
        st.success("This review is marked finished. Thanks for using the tool.")
        return

    st.header("6. Revise this review (optional)")
    st.caption(
        "Download the Excel above, add comments in the 'Reviewer Comments' column against any "
        "rows you want revisited, then re-upload it here. You can also add an overarching "
        "comment below. Do either, both, or neither."
    )
    reuploaded = st.file_uploader("Re-upload annotated Excel (optional)", type=["xlsx"], key=f"reup_{iteration}")
    overarching = st.text_area("Overarching comment for this iteration (optional)", key=f"over_{iteration}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Revise Review", disabled=config is None):
            reviewer_comments = {}
            if reuploaded is not None:
                file_job_id, comments, error = read_reviewer_comments(reuploaded.read())
                if error:
                    st.error(error)
                    return
                if file_job_id and file_job_id != job_id:
                    st.error("This Excel file belongs to a different review job. Upload the file "
                             "downloaded from THIS session instead.")
                    return
                reviewer_comments = comments
            _start_iteration_bg(job_id, config, overarching, reviewer_comments, iteration + 1)
            st.rerun()
    with col2:
        if st.button("I'm satisfied — Finish"):
            job_store.set_status(job_id, "finished", stage="Finished", current=1, total=1)
            st.rerun()


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def main():
    if not render_password_gate():
        return

    config = render_llm_sidebar()

    job_id = st.query_params.get("job_id") or st.session_state.get("job_id")
    if job_id and not job_store.job_exists(job_id):
        st.warning("That job no longer exists (it may have expired after 72 hours). Starting fresh.")
        job_id = None
        st.query_params.clear()

    if not job_id:
        render_upload_screen(config)
        return

    st.session_state["job_id"] = job_id
    status = job_store.get_status(job_id)

    if status["status"] == "error":
        st.error(f"Something went wrong: {status['error_message']}")
        _debug_log_download_button(job_id, key="error_screen_debug_log")
        if st.button("Start over"):
            st.session_state.pop("job_id", None)
            st.query_params.clear()
            st.rerun()
        return

    if status["status"] in ("uploaded", "summarizing"):
        render_progress_screen(job_id, status)
        return

    if status["status"] == "summarized":
        render_summary_screen(job_id, config)
        return

    if status["status"] in ("reviewing", "iterating"):
        render_progress_screen(job_id, status)
        return

    if status["status"] in ("review_done", "finished"):
        with st.expander("Project summary", expanded=False):
            st.markdown(job_store.get_summary(job_id))
        render_results_screen(job_id, config, status["iteration"], finished=status["status"] == "finished")
        return

    if status["status"] == "cancelled":
        st.warning("You cancelled this review.")
        if status["iteration"] > 0:
            st.caption("Findings from sections that completed before you cancelled are kept below.")
            with st.expander("Project summary", expanded=False):
                st.markdown(job_store.get_summary(job_id))
            render_results_screen(job_id, config, status["iteration"], finished=True)
        else:
            st.caption("Nothing completed before you cancelled - there's nothing to show or download.")
            if st.button("Start a new job"):
                st.session_state.pop("job_id", None)
                st.query_params.clear()
                st.rerun()
        return


if __name__ == "__main__":
    main()
