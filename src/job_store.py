"""Job persistence: a lightweight SQLite index (status/progress, queryable for
retention cleanup) plus on-disk files per job for the larger blobs (uploaded
PDFs, generated Excel files, findings JSON, summary text).

Enables resumable job URLs (?job_id=...) and the iterate-until-satisfied loop
without requiring login. No API keys are ever stored here (BYOK keys live only
in st.session_state for the browser session).
"""

import dataclasses
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from src.config import DATA_DIR, JOBS_DB_PATH, RETENTION_HOURS
from src.review_pipeline import Finding

JOBS_DIR = os.path.join(DATA_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)


@contextmanager
def _db():
    conn = sqlite3.connect(JOBS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                created_at REAL,
                updated_at REAL,
                status TEXT,
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                progress_stage TEXT DEFAULT '',
                error_message TEXT,
                iteration INTEGER DEFAULT 0
            )
        """)


# Guaranteed at import time, not lazily inside create_job() - job_exists() and
# every other reader can be (and is) called before any job has ever been
# created in a fresh database (e.g. an old ?job_id=... still in the browser's
# URL/session after a container restart wiped the previous SQLite file), and
# must not crash with "no such table: jobs" when that happens. Idempotent
# (CREATE TABLE IF NOT EXISTS), so calling it here is free.
init_db()


def _job_dir(job_id: str) -> str:
    d = os.path.join(JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "input"), exist_ok=True)
    return d


def create_job() -> str:
    job_id = str(uuid.uuid4())
    now = time.time()
    with _db() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, created_at, updated_at, status) VALUES (?, ?, ?, ?)",
            (job_id, now, now, "uploaded"),
        )
    _job_dir(job_id)
    return job_id


def job_exists(job_id: str) -> bool:
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row is not None


def set_status(job_id: str, status: str, stage: str = "", current: int = 0,
               total: int = 0, error: Optional[str] = None):
    with _db() as conn:
        conn.execute(
            """UPDATE jobs SET status=?, progress_stage=?, progress_current=?,
               progress_total=?, error_message=?, updated_at=? WHERE job_id=?""",
            (status, stage, current, total, error, time.time(), job_id),
        )


def set_iteration(job_id: str, iteration: int):
    with _db() as conn:
        conn.execute("UPDATE jobs SET iteration=?, updated_at=? WHERE job_id=?",
                     (iteration, time.time(), job_id))


def get_status(job_id: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


# --- Uploaded files ----------------------------------------------------------

def save_uploaded_files(job_id: str, files: list[tuple[str, bytes]]):
    input_dir = os.path.join(_job_dir(job_id), "input")
    for filename, content in files:
        safe_name = os.path.basename(filename)
        with open(os.path.join(input_dir, safe_name), "wb") as f:
            f.write(content)


def get_uploaded_files(job_id: str) -> list[tuple[str, bytes]]:
    input_dir = os.path.join(_job_dir(job_id), "input")
    files = []
    for filename in sorted(os.listdir(input_dir)):
        with open(os.path.join(input_dir, filename), "rb") as f:
            files.append((filename, f.read()))
    return files


# --- Summary -----------------------------------------------------------------

def save_summary(job_id: str, text: str):
    with open(os.path.join(_job_dir(job_id), "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text)


def get_summary(job_id: str) -> Optional[str]:
    path = os.path.join(_job_dir(job_id), "summary.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# --- Extra per-job instructions ----------------------------------------------

def save_extra_instructions(job_id: str, text: str):
    with open(os.path.join(_job_dir(job_id), "extra_instructions.txt"), "w", encoding="utf-8") as f:
        f.write(text)


def get_extra_instructions(job_id: str) -> Optional[str]:
    path = os.path.join(_job_dir(job_id), "extra_instructions.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# --- Stage timing (for the "is it stuck?" elapsed-time readout) --------------

def set_stage_start(job_id: str):
    with open(os.path.join(_job_dir(job_id), "stage_started_at.txt"), "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def get_stage_start(job_id: str) -> Optional[float]:
    path = os.path.join(_job_dir(job_id), "stage_started_at.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return float(f.read().strip())


def save_run_stats(job_id: str, iteration: int, stats: dict):
    """Small summary of the just-completed run - total duration, chunk count,
    concurrency used - shown on the results screen and included in the debug
    log for future latency questions."""
    path = os.path.join(_job_dir(job_id), f"run_stats_v{iteration}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)


def get_run_stats(job_id: str, iteration: int) -> Optional[dict]:
    path = os.path.join(_job_dir(job_id), f"run_stats_v{iteration}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- Findings & Excel per iteration ------------------------------------------

def save_findings(job_id: str, iteration: int, findings: list[Finding]):
    path = os.path.join(_job_dir(job_id), f"findings_v{iteration}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(fnd) for fnd in findings], f, indent=2)


def get_findings(job_id: str, iteration: int) -> Optional[list[Finding]]:
    path = os.path.join(_job_dir(job_id), f"findings_v{iteration}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [Finding(**item) for item in raw]


def save_warnings(job_id: str, iteration: int, warnings: list[str]):
    """Non-fatal issues from this iteration's run (e.g. a chunk whose response
    got truncated) - surfaced to the user alongside the findings that DID
    come back cleanly, rather than failing the whole job.
    """
    path = os.path.join(_job_dir(job_id), f"warnings_v{iteration}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(warnings, f, indent=2)


def get_warnings(job_id: str, iteration: int) -> list[str]:
    path = os.path.join(_job_dir(job_id), f"warnings_v{iteration}.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- Cooperative cancellation --------------------------------------------------
# Streamlit's own "Stop" only interrupts the current script rerun, not a
# background thread doing a real network call - it can't reach these at all.
# This flag is checked between chunk completions in run_first_pass: it stops
# any NOT-YET-STARTED queued chunk calls from ever running, but calls already
# in flight (bounded by CHUNK_CONCURRENCY, and now by LLM_REQUEST_TIMEOUT_SECONDS)
# finish naturally - a raw Python thread can't be safely force-killed mid-call.

def request_cancel(job_id: str):
    with open(os.path.join(_job_dir(job_id), "cancel_requested.flag"), "w") as f:
        f.write("1")


def is_cancel_requested(job_id: str) -> bool:
    return os.path.exists(os.path.join(_job_dir(job_id), "cancel_requested.flag"))


def clear_cancel(job_id: str):
    path = os.path.join(_job_dir(job_id), "cancel_requested.flag")
    if os.path.exists(path):
        os.remove(path)


# --- Debug log ---------------------------------------------------------------
# Every error/warning event gets appended here (full traceback, raw LLM
# response where available, which stage/chunk it happened in). Never includes
# the API key or uploaded PDF binary content - only filenames/page counts and
# whatever the pipeline itself generated (prompts, responses, findings).
# Downloadable as one bundle so the user can hand it over instead of
# copy-pasting a truncated error message.

def append_debug_event(job_id: str, event: dict):
    event = {**event, "timestamp": time.time()}
    path = os.path.join(_job_dir(job_id), "debug_log.json")
    events = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                events = json.load(f)
            except json.JSONDecodeError:
                events = []
    events.append(event)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, default=str)


def has_debug_log(job_id: str) -> bool:
    return os.path.exists(os.path.join(_job_dir(job_id), "debug_log.json"))


def get_debug_log_bytes(job_id: str) -> bytes:
    path = os.path.join(_job_dir(job_id), "debug_log.json")
    events = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            events = json.load(f)
    bundle = {
        "_note": (
            "Debug log for this review job. Does NOT include your API key or the "
            "uploaded PDF file contents (only filenames/page counts are logged). "
            "Safe to share for debugging - paste this whole file to Claude Code."
        ),
        "job_id": job_id,
        "events": events,
    }
    return json.dumps(bundle, indent=2, default=str).encode("utf-8")


# --- Iteration-attempt errors (kept separate from job-fatal errors) ----------
# A failed revision attempt should not strand the user without access to their
# last GOOD iteration's findings/Excel - those files are untouched on disk.
# This is a one-shot banner: read once by the results screen, then cleared.

def save_iteration_error(job_id: str, message: str):
    with open(os.path.join(_job_dir(job_id), "iteration_error.txt"), "w", encoding="utf-8") as f:
        f.write(message)


def pop_iteration_error(job_id: str) -> Optional[str]:
    path = os.path.join(_job_dir(job_id), "iteration_error.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        message = f.read()
    os.remove(path)
    return message


def save_excel(job_id: str, iteration: int, excel_bytes: bytes) -> str:
    path = os.path.join(_job_dir(job_id), f"output_v{iteration}.xlsx")
    with open(path, "wb") as f:
        f.write(excel_bytes)
    return path


def get_excel(job_id: str, iteration: int) -> Optional[bytes]:
    path = os.path.join(_job_dir(job_id), f"output_v{iteration}.xlsx")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


# --- Retention cleanup ---------------------------------------------------------

def cleanup_expired(retention_hours: int = RETENTION_HOURS):
    cutoff = time.time() - retention_hours * 3600
    with _db() as conn:
        rows = conn.execute("SELECT job_id FROM jobs WHERE created_at < ?", (cutoff,)).fetchall()
        expired_ids = [row["job_id"] for row in rows]
        if expired_ids:
            conn.executemany("DELETE FROM jobs WHERE job_id=?", [(j,) for j in expired_ids])
    for job_id in expired_ids:
        job_dir = os.path.join(JOBS_DIR, job_id)
        if os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
    return expired_ids
