"""Regression test for a real production incident: a background review
thread held a stale reference to job_store (captured before a hot-reload
added append_debug_event), so logging a chunk error raised AttributeError -
and the OUTER exception handler, trying to log THAT failure via the same
broken call, raised the identical error again. The double-fault meant the
code never reached job_store.set_status(..., "error", ...), so the job was
stuck at status="reviewing" forever with no way for the UI to ever learn it
had died. Streamlit's "Stop" button could not help, because nothing was
still running - the thread had already crashed.

Fix: _log_error() must never raise, no matter what fails inside it, so the
set_status(..., "error", ...) call that follows it always executes.

Run with: python -m pytest tests/ (or just: python tests/test_error_reporting_resilience.py)
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402 - path insert must happen first


def test_log_error_swallows_a_broken_job_store_append_debug_event():
    """Reproduces the exact incident: job_store.append_debug_event raises
    AttributeError (as it did for real, from the stale-module reference).
    _log_error must absorb this and return normally - not propagate."""
    with patch("app.job_store.append_debug_event",
               side_effect=AttributeError("module 'src.job_store' has no attribute 'append_debug_event'")):
        try:
            app._log_error("fake-job-id", "reviewing", RuntimeError("original failure"))
        except Exception as exc:  # noqa: BLE001
            assert False, f"_log_error must never raise, but raised: {exc!r}"
    print("test_log_error_swallows_a_broken_job_store_append_debug_event: OK")


def test_log_error_swallows_any_exception_type_not_just_attributeerror():
    """Generalize beyond the specific incident - _log_error's job is to be a
    safe no-op on failure, for ANY reason (disk full, permission error,
    whatever), not just this one historical cause."""
    for exc_to_raise in (AttributeError("x"), OSError("disk full"), ValueError("bad data")):
        with patch("app.job_store.append_debug_event", side_effect=exc_to_raise):
            try:
                app._log_error("fake-job-id", "stage", RuntimeError("original"))
            except Exception as exc:  # noqa: BLE001
                assert False, f"_log_error must never raise (case {exc_to_raise!r}), but raised: {exc!r}"
    print("test_log_error_swallows_any_exception_type_not_just_attributeerror: OK")


if __name__ == "__main__":
    test_log_error_swallows_a_broken_job_store_append_debug_event()
    test_log_error_swallows_any_exception_type_not_just_attributeerror()
    print("All tests passed.")
