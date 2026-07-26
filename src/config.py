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
# Was 20000 (bumped from an original 8192 to reduce truncation on a busy
# chunk). Reduced back down after two real incidents where a single large
# chunk's call ran ~908s and failed - evidence points to a fixed ~900s
# external cap on total request duration that streaming does NOT protect
# against (see README "Incident postmortem"). A smaller max_tokens directly
# bounds worst-case generation time. If truncation becomes a live problem
# again at this value, the fix is DEFAULT_CHUNK_CHAR_BUDGET (fewer findings
# needed per call) before this number - re-raising REVIEW_MAX_TOKENS
# re-opens the risk this reduction closes.
REVIEW_MAX_TOKENS = 12000

# Explicit per-request timeout. Previously unset (SDK defaults, which can run
# several minutes) - a single stuck/slow call could silently eat most of a
# job's wall-clock with nothing forcing it to fail into the retry/skip path.
# 150s is generous for a legitimately long reasoning-heavy response but bounds
# the worst case.
LLM_REQUEST_TIMEOUT_SECONDS = 150

# How many chunk calls run concurrently in the first-pass review loop (see
# review_pipeline.run_first_pass). This is THE main latency lever - wall-clock
# for the review stage is roughly (chunk_count / CHUNK_CONCURRENCY) * per-call
# time instead of chunk_count * per-call time. Kept conservative by default
# since BYOK means we don't know the user's actual provider rate-limit tier;
# raise it if you're on a higher tier and hitting few/no rate-limit errors in
# the debug log's chunk_timings.
CHUNK_CONCURRENCY = 4

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
