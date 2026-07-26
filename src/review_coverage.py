"""Build a transparent page-level completeness manifest for a review run."""

from src.pdf_processing import compress_page_numbers, describe_pages


def _page_numbers(items: list[dict]) -> set[int]:
    numbers: set[int] = set()
    for item in items:
        numbers.update(int(value) for value in item.get("page_numbers", []) if value is not None)
    return numbers


def build_review_coverage(chunks: list[list], chunk_errors: list[dict],
                          chunk_timings: list[dict]) -> dict:
    """Return page-level complete/partial/failed coverage plus chunk locators.

    A failed adaptive child identifies the exact leaf page range that was not
    recovered. Sibling pages that completed are not incorrectly counted as
    failed. When the provider returned some complete findings but the response
    was cut off, the affected leaf range is marked ``partial`` rather than
    complete because the findings are not guaranteed exhaustive.
    """
    errors_by_chunk = {item["chunk"]: item for item in chunk_errors}
    timings_by_chunk = {item["chunk"]: item for item in chunk_timings}
    entries: list[dict] = []
    complete_pages: set[tuple[str, int]] = set()
    partial_pages: set[tuple[str, int]] = set()
    failed_pages: set[tuple[str, int]] = set()

    for index, chunk in enumerate(chunks, start=1):
        descriptor = describe_pages(chunk)
        source_file = descriptor.get("source_file", "")
        all_numbers = set(descriptor.get("page_numbers") or [])
        error = errors_by_chunk.get(index)
        timing = timings_by_chunk.get(index, {})

        chunk_failed: set[int] = set()
        chunk_partial: set[int] = set()
        if error:
            failed_parts = error.get("failed_parts") or []
            if failed_parts:
                for part in failed_parts:
                    numbers = set(part.get("page_numbers") or [])
                    if part.get("partial_findings_kept", 0):
                        chunk_partial.update(numbers)
                    else:
                        chunk_failed.update(numbers)
            elif timing.get("partial") or error.get("partial_findings_kept", 0):
                chunk_partial.update(all_numbers)
            else:
                chunk_failed.update(all_numbers)

            # Parent-level salvage is the one case where recovered objects
            # cannot be mapped safely to the successful/failed child ranges.
            # Otherwise the failed leaf descriptors let us keep successful
            # sibling pages marked complete.
            if error.get("parent_salvage_used"):
                chunk_partial.update(all_numbers)
                chunk_failed.clear()

        chunk_partial -= chunk_failed
        chunk_complete = all_numbers - chunk_failed - chunk_partial
        complete_pages.update((source_file, page) for page in chunk_complete)
        partial_pages.update((source_file, page) for page in chunk_partial)
        failed_pages.update((source_file, page) for page in chunk_failed)

        if error is None:
            status = "complete"
        elif chunk_failed and not chunk_partial and not chunk_complete:
            status = "failed"
        else:
            status = "partial"

        entries.append({
            "chunk": index,
            "status": status,
            **descriptor,
            "complete_page_numbers": sorted(chunk_complete),
            "complete_page_label": compress_page_numbers(sorted(chunk_complete)),
            "partial_page_numbers": sorted(chunk_partial),
            "partial_page_label": compress_page_numbers(sorted(chunk_partial)),
            "failed_page_numbers": sorted(chunk_failed),
            "failed_page_label": compress_page_numbers(sorted(chunk_failed)),
            "partial_findings_kept": (error or {}).get("partial_findings_kept", 0),
            "failed_parts": (error or {}).get("failed_parts", []),
            "response_hints": (error or {}).get("response_hints", []),
            "technical_error": (error or {}).get("error", ""),
        })

    total_pages = len(complete_pages | partial_pages | failed_pages)
    return {
        "total_pages": total_pages,
        "complete_pages": len(complete_pages),
        "partial_pages": len(partial_pages),
        "failed_pages": len(failed_pages),
        "is_complete": not partial_pages and not failed_pages,
        "entries": entries,
    }
