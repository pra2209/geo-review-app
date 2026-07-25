# Geotechnical Report Review App (MVP)

Uploads a project's geotechnical report PDF(s), summarizes them, runs the
14-step review framework against them via an LLM, and outputs findings in the
MODON CRS Excel format — with an iterate-until-satisfied revision loop.

Built per `APP_BUILD_PLAN_v1.md` in the DP05 project folder. This README
covers running it, not the product decisions — see that plan doc for those.

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml and set ACCESS_CODE to something real

streamlit run app.py
```

Open the URL Streamlit prints. Enter the access code, paste your own
Anthropic or OpenAI API key in the sidebar, validate it, then upload PDFs.

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (public repo is fine — no secrets or project data
   live in the code; secrets are entered per-deploy in Streamlit's dashboard,
   and uploaded PDFs/keys never get committed).
2. On share.streamlit.io, point a new app at this repo, `app.py` as the entry
   point.
3. In the app's "Secrets" settings, paste the same keys as
   `.streamlit/secrets.toml` (at minimum `ACCESS_CODE`).
4. Deploy. Share the URL + access code with whoever should use it.

## What's intentionally simplified in this MVP (read before relying on it)

- **Anthropic Zero Data Retention / enterprise agreement**: not in place.
  Real client-confidential PDFs should not go through this app until that's
  sorted — see decision log in `APP_BUILD_PLAN_v1.md` §0. Use synthetic/
  non-sensitive sample reports for now.
- **Iteration re-review scope**: the revision pass re-reviews against the
  document *digest* (first ~20 pages per file) plus your prior findings and
  comments — not a full re-chunk of the entire document. Good for resolving/
  revising existing findings and applying a new instruction against material
  the digest covers. A request like "also check every appendix table in
  detail" on iteration 2 may need a fresh first-pass review instead. v2
  improvement: full re-chunk on iteration.
- **Re-uploaded Excel**: only edits to the `Reviewer Comments` column against
  *existing* rows are read back. Rows a user adds by hand (no hidden Finding
  ID) are ignored. v2 improvement: support new user-added rows.
- **Background jobs run as threads inside the single Streamlit process** —
  fine for a small internal tool, not built for high concurrent load. If this
  gets real traffic, move to a proper task queue (Redis + RQ/Celery).
- **Streamlit Community Cloud containers can sleep/restart on inactivity**,
  which would interrupt an in-progress job. Acceptable for MVP; worth
  upgrading hosting if long jobs + reliability both matter later.
- **Job data (uploaded PDFs, generated Excel files) auto-deletes after 72
  hours** (`RETENTION_HOURS` in `src/config.py`). Nothing is deleted
  proactively on "Finish" — cleanup runs on the retention sweep only. Wire
  `job_store.cleanup_expired()` into a scheduled call (e.g. a cron hitting a
  cleanup endpoint, or trigger it opportunistically at app startup) before
  relying on this in production.
- **Model allowlist** lives in `src/config.py::ALLOWED_MODELS` — update it
  there as new model generations ship.
- **No job history/browsing UI** — a job is only reachable via its
  `?job_id=...` URL, which is intentional (matches "no login," per the plan).
  There is no list-all-jobs screen.

## Project layout

```
app.py                        Streamlit UI + screen flow (single file, see note below)
src/config.py                 Model allowlist, file limits, retention window, paths
src/pdf_processing.py         PDF -> per-page text, digest, chunking
src/summary.py                Project summary generation
src/review_pipeline.py        Core 14-step review + iteration logic, Finding dataclass
src/excel_io.py                Write/read the MODON CRS Excel format
src/job_store.py              SQLite job index + per-job files on disk
src/llm_client.py             Provider-agnostic LLM call + model allowlist enforcement
src/providers/                Anthropic / OpenAI thin wrappers
framework/review_instructions.md   The 14-step logic, as fed to the LLM (edit this to
                                    change review behavior — no code change needed)
```

`app.py` is one file by design for MVP readability. If screens keep growing,
split into `screens/upload.py`, `screens/review.py`, etc., each taking
`job_id`/`config` and returning nothing (side effects via `st.*` calls) —
the current functions are already structured to make that split easy later.

## Known TODOs before this is "done" rather than "working"

- No automated tests yet (`tests/` exists but is empty) — at minimum, unit
  test `excel_io` round-trip (write then read back the same findings) and
  `review_pipeline._extract_json_array` against malformed LLM output.
- No retry/backoff on LLM calls — a transient API error currently fails the
  whole job with a raw error message shown to the user.
- No rate limiting / abuse protection beyond the shared access code.
