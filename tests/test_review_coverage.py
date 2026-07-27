"""Regression tests for transparent page-level review coverage."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pdf_processing import Page
from src.review_coverage import build_review_coverage


def _pages(start: int, end: int) -> list[Page]:
    return [Page("report.pdf", page, f"{page}.1 Section heading\ncontent") for page in range(start, end + 1)]


def test_incident_shape_maps_to_12_complete_12_failed_3_partial_pages():
    chunks = [_pages(1, 12), _pages(13, 24), _pages(25, 27)]
    errors = [
        {
            "chunk": 2,
            "partial_findings_kept": 0,
            "failed_parts": [
                {"label": "chunk 2 of 3.1", "page_numbers": list(range(13, 19)),
                 "page_label": "13-18", "partial_findings_kept": 0},
                {"label": "chunk 2 of 3.2", "page_numbers": list(range(19, 25)),
                 "page_label": "19-24", "partial_findings_kept": 0},
            ],
            "response_hints": [{
                "page_or_item": "Page 21, Table 1-8",
                "section_no": "1.9.2",
                "snapshot": "Bearing capacity methods scatter 14x, unreconciled",
            }],
            "error": "thinking-only output",
        },
        {
            "chunk": 3,
            "partial_findings_kept": 5,
            "failed_parts": [
                {"label": "chunk 3 of 3", "page_numbers": [25, 26, 27],
                 "page_label": "25-27", "partial_findings_kept": 5},
            ],
            "response_hints": [{
                "page_or_item": "Page 27",
                "section_no": "1.14.4",
                "snapshot": "Flood risk section has no numbers",
            }],
            "error": "incomplete JSON",
        },
    ]
    timings = [
        {"chunk": 1, "partial": False},
        {"chunk": 2, "partial": False},
        {"chunk": 3, "partial": True},
    ]

    coverage = build_review_coverage(chunks, errors, timings)

    assert coverage["total_pages"] == 27
    assert coverage["complete_pages"] == 12
    assert coverage["failed_pages"] == 12
    assert coverage["partial_pages"] == 3
    assert coverage["is_complete"] is False
    assert coverage["entries"][1]["failed_page_label"] == "13-24"
    assert coverage["entries"][2]["partial_page_label"] == "25-27"


def test_successful_sibling_pages_remain_complete_when_one_leaf_fails():
    chunks = [_pages(1, 4)]
    errors = [{
        "chunk": 1,
        "partial_findings_kept": 2,
        "failed_parts": [{
            "label": "chunk 1 of 1.2",
            "page_numbers": [3, 4],
            "page_label": "3-4",
            "partial_findings_kept": 0,
        }],
        "error": "right child failed",
    }]
    timings = [{"chunk": 1, "partial": True}]

    coverage = build_review_coverage(chunks, errors, timings)

    assert coverage["complete_pages"] == 2
    assert coverage["partial_pages"] == 0
    assert coverage["failed_pages"] == 2
    assert coverage["entries"][0]["complete_page_label"] == "1-2"
    assert coverage["entries"][0]["failed_page_label"] == "3-4"
