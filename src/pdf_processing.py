"""PDF ingestion: extract page text, tag by source file, and split into
a "digest" (high-signal early pages, used for the project summary and as
cross-reference context during review) plus size-bounded chunks for the
detailed per-section review pass.

Multiple uploaded files are treated as ONE project corpus (per product
decision: GDR + referenced GIR etc. reviewed together), so page tags always
carry the source filename.
"""

from dataclasses import dataclass

import fitz  # PyMuPDF


@dataclass
class Page:
    source_file: str
    page_num: int  # 1-indexed
    text: str


DIGEST_PAGES_PER_FILE = 20
# ~20k tokens/chunk. Bumped up from an earlier 40_000 (~10k tokens): all three
# allowlisted providers now support ~1M-token context windows, so the old
# budget was chunking far more granularly than needed - each extra chunk is a
# full sequential-or-parallel LLM round trip, so fewer/bigger chunks directly
# cuts total review latency. See review_pipeline.run_first_pass for the other
# half of the latency fix (parallelizing the chunk calls).
DEFAULT_CHUNK_CHAR_BUDGET = 80_000


def extract_pages(filename: str, file_bytes: bytes) -> list[Page]:
    """Extract text per page from one PDF's raw bytes."""
    pages: list[Page] = []
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            pages.append(Page(source_file=filename, page_num=i, text=text))
    return pages


def extract_all(files: list[tuple[str, bytes]]) -> list[Page]:
    """files: list of (filename, bytes). Returns combined, order-preserved page list."""
    all_pages: list[Page] = []
    for filename, file_bytes in files:
        all_pages.extend(extract_pages(filename, file_bytes))
    return all_pages


def build_digest(pages: list[Page], pages_per_file: int = DIGEST_PAGES_PER_FILE) -> list[Page]:
    """First N pages of each source file - where scope, site description,
    stratigraphy, parameter tables, seismicity, and code references typically
    live. Used for the project summary and as lightweight cross-reference
    context injected alongside every detailed chunk during review.
    """
    by_file: dict[str, list[Page]] = {}
    for p in pages:
        by_file.setdefault(p.source_file, []).append(p)
    digest: list[Page] = []
    for filename, file_pages in by_file.items():
        digest.extend(file_pages[:pages_per_file])
    return digest


def chunk_pages(pages: list[Page], char_budget: int = DEFAULT_CHUNK_CHAR_BUDGET) -> list[list[Page]]:
    """Group pages into chunks that fit a character budget, never splitting a
    page across chunks. Chunk boundaries follow document order per file.
    """
    chunks: list[list[Page]] = []
    current: list[Page] = []
    current_chars = 0
    for p in pages:
        page_chars = len(p.text)
        if current and current_chars + page_chars > char_budget:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(p)
        current_chars += page_chars
    if current:
        chunks.append(current)
    return chunks


def render_pages_as_text(pages: list[Page]) -> str:
    """Flatten a page list into a single tagged text block for an LLM prompt."""
    parts = []
    for p in pages:
        parts.append(f"\n--- [{p.source_file} | page {p.page_num}] ---\n{p.text}")
    return "".join(parts)


def total_page_count(files: list[tuple[str, bytes]]) -> int:
    total = 0
    for _, file_bytes in files:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            total += doc.page_count
    return total
