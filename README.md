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
Anthropic, OpenAI, or Gemini API key in the sidebar, validate it, then
upload PDFs.

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (public repo is fine — no secrets or project data
   live in the code; secrets are entered per-deploy in Streamlit's dashboard,
   and uploaded PDFs/keys never get committed).
2. On share.streamlit.io, point a new app at this repo, `app.py` as the entry
   point.
3. In the app's "Secrets" settings, paste the same keys as
   `.streamlit/secrets.toml` (at minimum `ACCESS_CODE`).
4. Deploy. Share the URL + access code with whoever should use it.

### Letting teammates use it without their own API key

Set `SHARED_PROVIDER` / `SHARED_MODEL` / `SHARED_ANTHROPIC_API_KEY` (or
`SHARED_OPENAI_API_KEY`) in Secrets — see the commented example in
`.streamlit/secrets.toml.example`. Anyone behind the access code then gets a
working config automatically, with an option in the sidebar to override with
their own key instead. **Set a hard monthly spend cap + alert on that key in
the provider's console** — that cap is what actually bounds cost exposure,
not which product/UI is issuing the calls (Claude Code, claude.ai, and this
app all ultimately call the same metered Anthropic/OpenAI API — none of them
is a free backend for a separate application).

## Model & cost comparison (checked live, July 2026 — verify before relying on it, this moves fast)

| Provider / model | Input $/1M | Output $/1M | Notes |
|---|---|---|---|
| **Gemini 2.5 Pro** | $1.25 (≤200k ctx) | $10.00 | Cheapest of the three below; still GA/stable, genuine reasoning tier, not a Flash/Lite variant |
| Gemini 3.1 Pro Preview | $2.00 (≤200k ctx) | $12.00 | Newer/stronger, but *not* the cheap Gemini option — don't assume "Gemini" is uniformly cheaper |
| Claude Sonnet 5 (default) | $2 → $3 after 31 Aug 2026 | $10 → $15 after 31 Aug 2026 | Intro pricing window closing soon |
| GPT-4.1 | $2.00 | $8.00 | |

Gemini access: free tier at [aistudio.google.com](https://aistudio.google.com/apikey) works
with no billing setup, but your content is used to improve Google's products on that tier —
same tradeoff as any provider's free tier. Use paid tier for anything with real project data.

If cost is the main thing stopping your team from adopting this, **Gemini 2.5 Pro as the
shared team key** (see below) is the lowest-friction fix — cheaper per call than either
current default, still clears the reasoning-capability bar the allowlist enforces.

## Incident postmortem: jobs stuck forever at "reviewing"

A real deployed job got permanently stuck, unrecoverable by Streamlit's own
"Stop" button. Root cause, found from the Streamlit Cloud log (search it for
`AttributeError` and `Thread-25` if you want the raw evidence):

A background review thread held a **stale reference to the `job_store`
module** - captured when the thread started, before a hot-reload deployed a
newer `job_store.py` with a new function (`append_debug_event`) that the
thread's copy of `app.py` expected to exist. When the thread tried to log a
chunk error, it hit `AttributeError: module 'src.job_store' has no attribute
'append_debug_event'`. The outer exception handler then tried to log *that*
failure through the exact same broken call and got the **identical error
again**. That double-fault meant execution never reached the
`job_store.set_status(job_id, "error", ...)` line - the only thing that lets
the UI ever learn a job died. The job sat at `status="reviewing"` forever,
not because anything was slow or hung, but because the one thread responsible
for reporting its own failure crashed before it could report anything.
Streamlit's Stop button could not help - there was nothing left running to
stop.

Two fixes, both shipped:

1. **`_log_error()` can now never raise** (`app.py`) - wrapped in its own
   try/except, so a failure while *logging* an error can never prevent
   *reporting* it. This is the actual fix; regression test in
   `tests/test_error_reporting_resilience.py` reproduces the exact
   `AttributeError` and proves it no longer propagates.
2. **Cancel / Abandon controls on the progress screen** - since a background
   thread can die (or a deploy can land) at any point with no way for the
   browser to know, there's now an explicit way out regardless of cause: a
   Cancel button (cooperative - stops any not-yet-started chunk calls,
   verified in `tests/test_parallel_review.py` by measuring that only a
   bounded few of 8 queued chunks ever run after cancelling) and an Abandon
   button (immediately clears the stuck job from the browser session so you
   can start a new one, regardless of what the old background thread is
   still doing).

**Residual risk, not fully closed**: this class of bug - a long-lived thread
holding stale references across a hot-reload - can resurface in a different
shape if a future code change introduces the same pattern elsewhere. The
`_log_error` fix specifically hardens the one path that made THIS incident
unrecoverable; it doesn't prevent every possible consequence of deploying new
code while a background thread from older code is still running. Practical
mitigation: avoid pushing to `main` while a job might be actively running on
the deployed app, and if the app ever seems stuck after a fresh deploy, use
**Manage app → Reboot app** rather than waiting - a full restart clears any
stale in-memory state that a code push alone does not.

**A second, separate crash signature was also present in that same log**,
later in the timeline: repeated `KeyError` inside `importlib`'s
`_find_and_load` / `_load_unlocked`, immediately after a `🔄 Updated app!`
deploy marker, cascading through a different module on each subsequent
rerun. This is import-cache corruption from a hot-reload racing Python
3.14's import system (Streamlit Cloud's runtime version at the time) - not
the same bug as above, and not something fixable in application code. If you
see this specific `KeyError`-from-`importlib` signature in the logs: same
recovery (Reboot app), different cause (platform-level, not the
`_log_error` issue).

## Latency

For a large multi-hundred-page PDF, review time was originally dominated by
strictly sequential chunk processing - one full LLM round trip after another,
with no explicit request timeout. Fixed via:

- **Concurrent chunk processing** (`CHUNK_CONCURRENCY` in `src/config.py`,
  default 4): chunks run through a bounded worker pool instead of one at a
  time - wall-clock is roughly `chunk_count / CHUNK_CONCURRENCY * per-call-time`
  instead of `chunk_count * per-call-time`. Findings are restored to original
  document order in the output regardless of which chunk's call finished
  first (`tests/test_parallel_review.py` proves this with reversed completion
  order, and separately proves the concurrency is real - 3 mocked 0.2s chunks
  finish in ~0.2s total, not ~0.6s).
- **Larger chunk budget** (`DEFAULT_CHUNK_CHAR_BUDGET` in `src/pdf_processing.py`,
  40k → 80k chars): all three allowlisted providers now support ~1M-token
  context windows, so the old budget was chunking - and round-tripping - far
  more granularly than needed.
- **One short retry per chunk** on failure (`_process_one_chunk` in
  `review_pipeline.py`) before giving up - covers transient/rate-limit blips,
  which are more likely now that requests go out concurrently.
- **Explicit request timeout** (`LLM_REQUEST_TIMEOUT_SECONDS`, 150s) on every
  provider call - previously unset, relying on SDK defaults that can run
  several minutes, meaning one stuck call could quietly eat most of a job's
  wall-clock.
- **Per-chunk timing capture**: every chunk's duration (success or failure)
  is logged to the debug log, and a "Completed in Xm Ys across N chunks"
  summary shows on the results screen - so a future "why did this take so
  long" question comes with real data, not a guess. The progress screen also
  shows live elapsed time.

**Not built (needs a product decision, not just an engineering fix)**: a
"fast mode" that trades review depth for speed on very large documents -
e.g. coarser chunking or skipping low-signal boilerplate sections entirely.
That changes what the review actually covers, so it's a call for whoever
owns review quality, not something to decide unilaterally in code.

## Cost & reliability

- **Prompt caching**: the framework instructions (system prompt) and document
  digest are byte-identical across every chunk call within a job — both are
  marked as cacheable (`call_with_cache` in each provider module, used for
  all review/iteration calls via `src/llm_client.py`). Anthropic caches
  explicitly via `cache_control`; OpenAI and Gemini cache these same repeated
  prefixes automatically server-side. Meaningfully cuts cost on multi-chunk
  documents. Verified via mocked request-payload tests in
  `tests/test_prompt_caching.py` (can't verify an actual cache *hit* without
  a live key — that shows up in the provider's usage dashboard as
  `cache_read_input_tokens` / cached-token counts on the 2nd+ call).
- **Per-chunk resilience**: if one chunk's response gets cut off (hit the
  token limit) or otherwise fails to parse, that chunk's findings are
  skipped — every other chunk's findings (already paid for) are kept, not
  discarded. Partial results are surfaced as dismissable warnings on the
  results screen, not a job-killing error. Same principle applies to a failed
  "Revise Review" iteration: it falls back to your last successful result
  instead of a dead-end error screen.
- **Debug log**: any error or per-chunk warning gets a "Download error log"
  button next to it — a JSON bundle with full traceback, the raw model
  response where available, and provider/model (never your API key or PDF
  contents). Hand that file directly to Claude Code instead of copy-pasting a
  truncated on-screen message.

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
src/providers/                Anthropic / OpenAI / Gemini thin wrappers (Gemini via Google's
                               OpenAI-compatible endpoint, not a separate native SDK)
framework/review_instructions.md   The 14-step logic, as fed to the LLM (edit this to
                                    change review behavior — no code change needed)
```

`app.py` is one file by design for MVP readability. If screens keep growing,
split into `screens/upload.py`, `screens/review.py`, etc., each taking
`job_id`/`config` and returning nothing (side effects via `st.*` calls) —
the current functions are already structured to make that split easy later.

## Known TODOs before this is "done" rather than "working"

- Tests cover Excel round-trip, JSON-truncation salvage/error behavior,
  caching request payloads, and chunk concurrency/ordering/retry (`tests/`) —
  not yet: `pdf_processing`, `job_store`, or the Streamlit screens themselves
  (those were smoke-tested manually against a real DP05 report, not under
  automated test).
- One retry per chunk, not a full retry-with-backoff policy — a chunk that
  fails twice is skipped and surfaced as a warning; retrying it beyond that
  means re-running the review or a Revise Review iteration, not further
  automatic in-job retries.
- No rate limiting / abuse protection beyond the shared access code — worth
  revisiting alongside `CHUNK_CONCURRENCY` if this sees real multi-user
  traffic, since concurrent requests per job now multiply by concurrent
  users too.
