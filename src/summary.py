"""Quick project summary generation - the first thing the user sees after
upload, before any review starts. Purpose: let the user confirm the app
actually understood the document before committing to a full review run.
"""

from src.llm_client import LLMConfig, call
from src.pdf_processing import Page, render_pages_as_text

SUMMARY_SYSTEM_PROMPT = """You are summarizing a geotechnical report for an engineer who is
about to review it in detail. Produce a concise, high-signal summary (aim for 1-2 pages of
text, roughly 500-900 words) covering, where present in the source text:

- Project name, location, client/consultant, report type and date/revision
- Site description: area, topography, elevation range, number and type of structures
- Site investigation: number of boreholes, depths, dates, key testing performed
- Soil/rock stratigraphy: main layers encountered, in order
- Groundwater: depth/elevation range, number of measurement points
- Seismicity: zone, PGA, site class if stated
- Design codes/standards referenced
- Key structures being designed: foundation types, retaining walls, slopes - whatever applies
- Headline numbers if present: bearing pressures, settlement predictions, safety factors

Write in plain prose with short paragraphs and a few bullet lists where it aids scanning. Do not
invent information not present in the source text - if something isn't stated, say so briefly
rather than guessing. This is a factual summary, not a review; do not flag issues here."""


def generate_summary(config: LLMConfig, digest_pages: list[Page]) -> str:
    document_text = render_pages_as_text(digest_pages)
    user_prompt = (
        "Here is the extracted text from the opening pages of the uploaded report(s) "
        "(multiple files, if present, are one project's material and are tagged by source "
        f"file/page):\n\n{document_text}\n\nProduce the summary now."
    )
    return call(config, SUMMARY_SYSTEM_PROMPT, user_prompt, max_tokens=2000)
