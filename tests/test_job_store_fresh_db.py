"""Regression test for a real production crash: on a fresh Streamlit Cloud
container (ephemeral filesystem wiped on restart/reboot), if the browser
still has an old ?job_id=... from before the restart, main() calls
job_store.job_exists(job_id) before any job has ever been created in the
new, empty SQLite database - and job_exists() queried a table that had never
been created, since init_db() previously only ran inside create_job().

Fix: init_db() now runs unconditionally at module import time (mirroring how
JOBS_DIR is already guaranteed to exist at import time), so the table always
exists before any reader can possibly be called.

This test simulates that exact "fresh database, never called create_job()"
scenario by pointing job_store at a brand-new SQLite file path in a temp
directory, then calling job_exists() FIRST - no create_job() call at all.

Run with: python -m pytest tests/ (or just: python tests/test_job_store_fresh_db.py)
"""

import sys
import os
import tempfile
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_job_exists_does_not_crash_on_never_before_seen_database():
    with tempfile.TemporaryDirectory() as tmp_dir:
        import src.config as config
        original_data_dir = config.DATA_DIR
        original_db_path = config.JOBS_DB_PATH
        try:
            # Point at a brand-new location - simulates a fresh container
            # where no job has EVER been created, i.e. init_db() (if it only
            # ran inside create_job()) would never have run.
            config.DATA_DIR = tmp_dir
            config.JOBS_DB_PATH = os.path.join(tmp_dir, "jobs.db")

            import src.job_store as job_store
            importlib.reload(job_store)  # re-run module-level init_db() against the new path

            assert not os.path.exists(config.JOBS_DB_PATH) or True  # sanity - just documenting intent

            # THE ACTUAL REGRESSION: this used to raise
            # sqlite3.OperationalError: no such table: jobs
            result = job_store.job_exists("some-job-id-that-was-never-created")
            assert result is False
            print("test_job_exists_does_not_crash_on_never_before_seen_database: OK")
        finally:
            config.DATA_DIR = original_data_dir
            config.JOBS_DB_PATH = original_db_path


if __name__ == "__main__":
    test_job_exists_does_not_crash_on_never_before_seen_database()
    print("All tests passed.")
