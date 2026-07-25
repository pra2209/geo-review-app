"""Round-trip test for the MODON CRS Excel writer/reader.

Run with: python -m pytest tests/ (or just: python tests/test_excel_io.py)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.excel_io import write_excel, read_reviewer_comments
from src.review_pipeline import Finding


def make_sample_findings():
    return [
        Finding(
            finding_id="f1", source_file="report.pdf", page_or_item="Page 34",
            section_no="5.8.5", snapshot="Settlement Exceeds Allowable Limits",
            discipline="Geotechnical",
            review_comment="Settlement predicted at 4.8cm vs 50mm allowable.",
            status="D", framework_step=11,
        ),
        Finding(
            finding_id="f2", source_file="report.pdf", page_or_item="Page 22",
            section_no="5.7", snapshot="Missing slope stability analysis",
            discipline="Geotechnical",
            review_comment="Only global stability shown, not per-segment.",
            status="C", framework_step=12,
        ),
    ]


def test_round_trip():
    findings = make_sample_findings()
    excel_bytes = write_excel(findings, job_id="job-123")
    assert len(excel_bytes) > 0

    # Simulate the user adding a reviewer comment by re-writing with one set.
    findings[0].reviewer_comment = "Agreed, please provide pile option cost."
    excel_bytes_v2 = write_excel(findings, job_id="job-123")

    job_id, comments, error = read_reviewer_comments(excel_bytes_v2)
    assert error is None, f"Unexpected error: {error}"
    assert job_id == "job-123"
    assert comments.get("f1") == "Agreed, please provide pile option cost."
    assert "f2" not in comments  # no comment was set on f2
    print("test_round_trip: OK")


def test_rejects_wrong_template():
    import openpyxl
    import io
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Not", "The", "Right", "Columns"])
    buf = io.BytesIO()
    wb.save(buf)

    job_id, comments, error = read_reviewer_comments(buf.getvalue())
    assert error is not None
    assert job_id is None
    print("test_rejects_wrong_template: OK")


if __name__ == "__main__":
    test_round_trip()
    test_rejects_wrong_template()
    print("All tests passed.")
