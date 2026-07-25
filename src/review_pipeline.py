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

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.config import FRAMEWORK_PATH
from src.llm_client import LLMConfig, call
from src.pdf_processing import Page, render_pages_as_text


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


def _extract_json_array(raw_text: str) -> list[dict]:
    """LLMs sometimes wrap JSON in markdown fences or add stray prose. Extract
    the first top-level JSON array robustly."""
    text = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse findings JSON from model output: {exc}\nRaw: {raw_text[:500]}")


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


def run_first_pass(config: LLMConfig, digest_pages: list[Page],
                    chunks: list[list[Page]],
                    extra_instructions: Optional[str] = None,
                    progress_cb=None) -> list[Finding]:
    """One LLM call per chunk. progress_cb(done, total) if provided, for the UI progress bar."""
    framework_text = load_framework()
    if extra_instructions:
        framework_text += (
            "\n\n## PER-JOB EXTRA INSTRUCTIONS (this job only, provided by the user)\n"
            f"{extra_instructions}\n"
        )

    digest_text = render_pages_as_text(digest_pages)
    all_findings: list[Finding] = []
    total = len(chunks)

    for i, chunk in enumerate(chunks, start=1):
        chunk_text = render_pages_as_text(chunk)
        user_prompt = (
            "PROJECT DIGEST (opening pages of each source file, for cross-reference / "
            f"traceability checks only - do not re-review this section on its own):\n{digest_text}\n\n"
            f"DETAILED SECTION TO REVIEW NOW (chunk {i} of {total}):\n{chunk_text}\n\n"
            "Review this chunk against all 14 steps that apply to its content, using the digest "
            "for traceability where needed. Return the JSON findings array now."
        )
        raw = call(config, framework_text, user_prompt, max_tokens=8192)
        items = _extract_json_array(raw)
        all_findings.extend(_findings_from_json(items))
        if progress_cb:
            progress_cb(i, total)

    return all_findings


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

    user_prompt = (
        "This is a REVISION pass (iteration mode - see instructions above).\n\n"
        f"PROJECT DIGEST:\n{digest_text}\n\n"
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
    raw = call(config, framework_text, user_prompt, max_tokens=8192)
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
