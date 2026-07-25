"""Regression tests for the JSON extraction bug reported against a real
Streamlit run: a model response that got cut off mid-generation (hit the
output token limit) crashed the whole chunk instead of salvaging whatever
findings HAD finished generating, or failing with a clear message when
nothing could be salvaged.

Run with: python -m pytest tests/ (or just: python tests/test_review_pipeline.py)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.review_pipeline import _extract_json_array, TruncatedResponseError

# This is the exact fragment from the user's reported error (their own
# trailing chat instruction to the assistant, which got concatenated onto
# the pasted error text with no separator, has been removed here - everything
# below the ```json fence is the real, verbatim truncated model output).
REAL_TRUNCATED_SAMPLE = '''```json
[
  {
    "source_file": "Geotechnical Report for DP05, Village - Wadi Yemm - Ras El-Hekma (CG26-52.1).pdf",
    "page_or_item": "Page 6-7",
    "section_no": "3",
    "snapshot": "Basement zone FLs given as ranges, not per-building",
    "discipline": "Geotechnical",
    "review_comment": "Section 3 lists a specific foundation level (FL) for every building on Steps 1, 2, 3 and 5 (e.g. Village-R07 to R09 at +2.45 m, Village-B04 to B06 at +13.50 m), but for the two basement zones on Step.'''


def test_well_formed_array_still_parses_normally():
    good = '''```json
[
  {"source_file": "a.pdf", "page_or_item": "Page 1", "section_no": "1",
   "snapshot": "Test", "discipline": "Geotechnical", "review_comment": "Fine.",
   "status": "A", "framework_step": 1}
]
```'''
    items = _extract_json_array(good)
    assert len(items) == 1
    assert items[0]["snapshot"] == "Test"
    print("test_well_formed_array_still_parses_normally: OK")


def test_real_reported_truncation_raises_clear_error_not_generic_crash():
    """This exact input used to raise 'Expecting value: line 1 column 1
    (char 0)' from json.loads() on the raw markdown-fenced text. It should
    now raise our specific TruncatedResponseError with an explanatory
    message, since nothing completed generating in this sample."""
    try:
        _extract_json_array(REAL_TRUNCATED_SAMPLE)
        assert False, "expected TruncatedResponseError"
    except TruncatedResponseError as exc:
        assert "hit the output token limit" in str(exc)
        print("test_real_reported_truncation_raises_clear_error_not_generic_crash: OK")


def test_truncation_after_some_complete_findings_salvages_them():
    """Simulates a longer response that completed 2 findings before getting
    cut off mid-way through a 3rd - the realistic shape of the bug on a
    busier chunk. The 2 complete findings must be returned, not discarded."""
    partial = '''```json
[
  {"source_file": "a.pdf", "page_or_item": "Page 5", "section_no": "2",
   "snapshot": "First finding", "discipline": "Geotechnical", "review_comment": "Fine.",
   "status": "B", "framework_step": 2},
  {"source_file": "a.pdf", "page_or_item": "Page 9", "section_no": "4",
   "snapshot": "Second finding", "discipline": "Geotechnical", "review_comment": "Also fine.",
   "status": "C", "framework_step": 4},
  {"source_file": "a.pdf", "page_or_item": "Page 12", "section_no": "5",
   "snapshot": "Third finding got cut off", "discipline": "Geotechnical",
   "review_comment": "This one never finish'''
    items = _extract_json_array(partial)
    assert len(items) == 2, f"expected 2 salvaged findings, got {len(items)}"
    assert items[0]["snapshot"] == "First finding"
    assert items[1]["snapshot"] == "Second finding"
    print("test_truncation_after_some_complete_findings_salvages_them: OK")


if __name__ == "__main__":
    test_well_formed_array_still_parses_normally()
    test_real_reported_truncation_raises_clear_error_not_generic_crash()
    test_truncation_after_some_complete_findings_salvages_them()
    print("All tests passed.")
