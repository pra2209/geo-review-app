"""Central config: model allowlist, size/count limits, retention window.

Keep this file the single source of truth for tunables so they don't scatter
through the codebase.
"""

# --- Model allowlist -------------------------------------------------------
# BYOK: user supplies their own key. We still gate which models are selectable
# so review quality doesn't degrade to a cheap/fast model that can't reliably
# do multi-step standards reasoning. Update this dict as new model generations
# ship - it's the only place that needs to change.

ALLOWED_MODELS = {
    "anthropic": [
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-sonnet-4-5",
        "claude-opus-4-5",
    ],
    "openai": [
        "gpt-5",
        "gpt-4.1",
        "o3",
        "o4-mini",
    ],
    "gemini": [
        "gemini-2.5-pro",
        "gemini-3.1-pro-preview",
    ],
}

DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4.1",
    # Cheaper than 3.1 Pro Preview and still a genuine reasoning-tier model
    # (not a Flash/Flash-Lite variant) - see cost comparison in README.
    "gemini": "gemini-2.5-pro",
}

# --- LLM call tuning ----------------------------------------------------------
# Bumped from an earlier 8192: a busy chunk with many findings (each review_comment
# runs 150-400 words per the framework instructions) can legitimately need more
# room, and a too-tight budget causes the model to cut a finding off mid-generation
# rather than finishing cleanly. See review_pipeline._extract_json_array for the
# complementary fix: even if truncation still happens, complete findings are
# salvaged instead of the whole chunk's output being discarded.
REVIEW_MAX_TOKENS = 16000

# --- Upload limits -----------------------------------------------------------
MAX_FILES_PER_JOB = 5
# Streamlit's own per-file/per-request cap is set in .streamlit/config.toml
# (maxUploadSize, in MB). This is a secondary sanity check in code.
MAX_FILE_SIZE_MB = 500

# --- Retention ---------------------------------------------------------------
RETENTION_HOURS = 72

# --- Paths ---------------------------------------------------------------
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
JOBS_DB_PATH = os.path.join(DATA_DIR, "jobs.db")
FRAMEWORK_PATH = os.path.join(BASE_DIR, "framework", "review_instructions.md")

os.makedirs(DATA_DIR, exist_ok=True)
