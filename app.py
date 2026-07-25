"""Geotechnical Report Review — Streamlit MVP.

Single-file app on purpose for v1 (see README for the v2 refactor note if this
grows). Screens, in order: password gate -> upload -> summary + framework
preview + extra instructions -> review progress -> results + iterate loop ->
finished.
"""

import threading
import time

import streamlit as st

from src.config import ALLOWED_MODELS, DEFAULT_MODEL, MAX_FILES_PER_JOB
from src import job_store
from src.excel_io import write_excel, read_reviewer_comments, DISCLAIMER
from src.llm_client import LLMConfig, validate_key
from src.pdf_processing import extract_all, build_digest, chunk_pages
from src.review_pipeline import load_framework, run_first_pass, run_iteration, Finding
from src.summary import generate_summary

st.set_page_config(page_title="Geotechnical Report Review", layout="wide")

STATUS_LABELS = {
    "D": "Rejected (critical)", "C": "Revise & Resubmit", "B": "Incorporate & Proceed",
    "A": "Proceed", "E": "Not Required",
}


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------

def render_password_gate() -> bool:
    if st.session_state.get("authenticated"):
        return True
    st.title("Geotechnical Report Review")
    code = st.text_input("Access code", type="password")
    if st.button("Enter"):
        # Falls back to a hardcoded default if Secrets isn't configured on the
        # deploy target yet - Secrets, when set, still takes precedence.
        expected = st.secrets.get("ACCESS_CODE", "richa@123")
        if expected and code == expected:
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
    provider = st.sidebar.selectbox("Provider", ["anthropic", "openai"])
    model = st.sidebar.selectbox("Model", ALLOWED_MODELS[provider],
                                  index=ALLOWED_MODELS[provider].index(DEFAULT_MODEL[provider]))
    api_key = st.sidebar.text_input("API key (yours - not stored)", type="password",
                                     value=st.session_state.get("api_key", ""))
    st.sidebar.caption(
        "Only reasoning-capable models are listed - small/fast models are excluded because "
        "this review requires multi-step standards reasoning."
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
            files = job_store.get_uploaded_files(job_id)
            pages = extract_all(files)
            digest = build_digest(pages)
            summary = generate_summary(config, digest)
            job_store.save_summary(job_id, summary)
            job_store.set_status(job_id, "summarized", stage="Summary ready", current=1, total=1)
        except Exception as exc:  # noqa: BLE001
            job_store.set_status(job_id, "error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Screen: summary + framework preview + extra instructions + start review
# ---------------------------------------------------------------------------

def render_summary_screen(job_id: str, config: LLMConfig | None):
    st.header("2. Project summary")
    summary = job_store.get_summary(job_id)
    st.markdown(summary)

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
            job_store.set_status(job_id, "reviewing", stage="Starting review...", current=0, total=1)
            files = job_store.get_uploaded_files(job_id)
            pages = extract_all(files)
            digest = build_digest(pages)
            chunks = chunk_pages(pages)

            def progress_cb(done, total):
                job_store.set_status(job_id, "reviewing",
                                      stage=f"Reviewing section {done} of {total}...",
                                      current=done, total=total)

            findings, chunk_errors = run_first_pass(
                config, digest, chunks, extra_instructions or None, progress_cb=progress_cb)

            if not findings and chunk_errors:
                # Every chunk failed - nothing to show, this really is fatal.
                job_store.set_status(job_id, "error",
                                      error="All chunks failed:\n" + "\n".join(chunk_errors))
                return

            job_store.save_findings(job_id, iteration, findings)
            job_store.save_warnings(job_id, iteration, chunk_errors)
            excel_bytes = write_excel(findings, job_id)
            job_store.save_excel(job_id, iteration, excel_bytes)
            job_store.set_iteration(job_id, iteration)
            stage = "Review complete" if not chunk_errors else (
                f"Review complete with {len(chunk_errors)} section(s) needing a retry - see warnings below")
            job_store.set_status(job_id, "review_done", stage=stage, current=1, total=1)
        except Exception as exc:  # noqa: BLE001
            job_store.set_status(job_id, "error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()


def _start_iteration_bg(job_id: str, config: LLMConfig, overarching_comment: str,
                         reviewer_comments: dict[str, str], next_iteration: int):
    def _run():
        try:
            job_store.set_status(job_id, "iterating", stage="Starting revision...", current=0, total=1)
            prior = job_store.get_findings(job_id, next_iteration - 1) or []
            for f in prior:
                if f.finding_id in reviewer_comments:
                    f.reviewer_comment = reviewer_comments[f.finding_id]

            files = job_store.get_uploaded_files(job_id)
            pages = extract_all(files)
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
            job_store.save_iteration_error(job_id, str(exc))
            job_store.set_status(job_id, "review_done",
                                  stage="Revision attempt failed - showing your last good result",
                                  current=1, total=1)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Screen: progress (polling)
# ---------------------------------------------------------------------------

def render_progress_screen(status: dict):
    st.header("Working...")
    total = max(status["progress_total"], 1)
    current = status["progress_current"]
    st.progress(current / total, text=status["progress_stage"] or "Working...")
    st.caption("This page auto-refreshes every couple of seconds.")
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

    st.header(f"5. Results (iteration {iteration})")
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
        if st.button("Start over"):
            st.session_state.pop("job_id", None)
            st.query_params.clear()
            st.rerun()
        return

    if status["status"] in ("uploaded", "summarizing"):
        render_progress_screen(status)
        return

    if status["status"] == "summarized":
        render_summary_screen(job_id, config)
        return

    if status["status"] in ("reviewing", "iterating"):
        render_progress_screen(status)
        return

    if status["status"] in ("review_done", "finished"):
        with st.expander("Project summary", expanded=False):
            st.markdown(job_store.get_summary(job_id))
        render_results_screen(job_id, config, status["iteration"], finished=status["status"] == "finished")
        return


if __name__ == "__main__":
    main()
