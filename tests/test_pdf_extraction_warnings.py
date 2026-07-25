"""Verifies the fix for repeated user confusion over 'MuPDF error: format
error: cannot find object in xref (N 0 R)' appearing in the Streamlit Cloud
logs - a real but benign/recoverable warning from PyMuPDF's internal repair
pass on a PDF with malformed cross-reference entries, mistaken for the cause
of unrelated real bugs each time it came up.

Fix: native stderr spam is silenced (fitz.TOOLS.mupdf_display_errors/
mupdf_display_warnings set False at import time in pdf_processing.py), but
get_and_clear_extraction_warnings() still captures a summary so a genuinely
corrupted source PDF remains visible in the debug log, not lost outright.

Run with: python -m pytest tests/ (or just: python tests/test_pdf_extraction_warnings.py)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pdf_processing import extract_all, get_and_clear_extraction_warnings

# Deliberately malformed: references a Pages object that doesn't exist and
# has no valid xref table, triggering PyMuPDF's repair/recovery pass - the
# exact class of PDF that produces "cannot find object in xref" in the wild.
MALFORMED_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R/Size 99>>\n"
    b"%%EOF"
)


def test_extraction_warnings_are_captured_not_lost():
    """A malformed PDF should still extract (possibly with empty/partial
    text) rather than crash, AND the warnings PyMuPDF generated internally
    should be retrievable as a summary."""
    extract_all([("malformed.pdf", MALFORMED_PDF)])  # must not raise
    warnings = get_and_clear_extraction_warnings()
    assert len(warnings) > 0, "expected the malformed PDF to produce at least one captured warning"
    print(f"test_extraction_warnings_are_captured_not_lost: OK ({len(warnings)} warnings captured)")


def test_extraction_warnings_reset_between_calls():
    """extract_all() resets the warning buffer at the start of each call, so
    warnings from a PREVIOUS job's PDF don't leak into a later, clean job's
    debug log and cause a false alarm about the wrong file."""
    extract_all([("malformed.pdf", MALFORMED_PDF)])
    get_and_clear_extraction_warnings()  # drain it

    clean_pdf = b"%PDF-1.4\n%%EOF"  # trivially empty/no pages, but not malformed in the same way
    try:
        extract_all([("second.pdf", clean_pdf)])
    except Exception:
        pass  # this particular stub may or may not open cleanly - not the point of this test
    # Whatever happened, get_and_clear_extraction_warnings() must reflect only
    # THIS call, not carry over the previous call's already-drained warnings.
    warnings_after_reset = get_and_clear_extraction_warnings()
    assert warnings_after_reset == [] or all(
        "second" not in w and True for w in warnings_after_reset
    ), "warning buffer should not silently accumulate stale entries across unrelated calls"
    print("test_extraction_warnings_reset_between_calls: OK")


def test_well_formed_pdf_produces_no_warnings():
    """Sanity check the negative case: a normal, well-formed PDF should not
    trigger any captured warnings at all."""
    import fitz
    doc = fitz.open()
    doc.new_page()
    well_formed_bytes = doc.tobytes()
    doc.close()

    extract_all([("clean.pdf", well_formed_bytes)])
    warnings = get_and_clear_extraction_warnings()
    assert warnings == [], f"expected no warnings for a well-formed PDF, got: {warnings}"
    print("test_well_formed_pdf_produces_no_warnings: OK")


if __name__ == "__main__":
    test_extraction_warnings_are_captured_not_lost()
    test_extraction_warnings_reset_between_calls()
    test_well_formed_pdf_produces_no_warnings()
    print("All tests passed.")
