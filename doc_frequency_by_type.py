#!/usr/bin/env python3
"""
For a given table_type (default 'generic'), optionally filtered to a
target section, count how many such tables come from EACH document,
sorted descending -- to find which documents are the heaviest
contributors, so we can go inspect those specific documents directly
instead of sampling randomly across the whole corpus.

This is a generalized version of doc_frequency_generic_genbg.py --
same logic, but --target-type is now a parameter instead of being
hardcoded to 'generic'.

Usage:
    # All 'generic' tables, any section (same as doc_frequency_generic_genbg.py with no --target-section)
    python3 doc_frequency_by_type.py --input-dir DIR

    # unknown_continuation tables, any section
    python3 doc_frequency_by_type.py --input-dir DIR --target-type unknown_continuation

    # unknown_continuation tables, restricted to one section
    python3 doc_frequency_by_type.py --input-dir DIR --target-type unknown_continuation --target-section "Coverage Policy"
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")

import pdfplumber

from cigna_parse import _extract_meta, _parse_toc_rcr
from cigna_constants import (
    FOOTER_PATTERNS, MM_SECTION_VOCAB, SECTION_VOCAB,
    CPG_SECTION_VOCAB, PH_SECTION_VOCAB,
)
from cigna_parse_tables import (
    find_table_info, _classify_table_types,
    _rescue_pref_criteria_continuations,
    _rescue_hcpcs_cpt_icd_continuations,
    inject_tables_into_tree,
    _revert_spurious_reasons_for_drug_family,
    _is_mm_family_by_prefix,
    _is_cpg_family, _is_ph_family,
    _apply_network_adequacy_override,
    _apply_dosing_table_override,
    _apply_three_step_medications_override,
    _apply_toc_table_override,
    _APPROVED_SPURIOUS_REASONS, _SPURIOUS_REASON_TYPES,
)
from cigna_extractor import (
    extract_raw_lines as _extract_raw_lines_ext,
    extract_paragraph_lines as _extract_para_lines,
    _find_section_boundaries,
    _has_underline_rect,
)


def find_section_for_table(table_info, section_boundaries):
    page = table_info.get("page")
    bbox = table_info.get("bbox")
    top = bbox[1] if bbox else None
    if isinstance(section_boundaries, list):
        candidates = []
        for b in section_boundaries:
            if isinstance(b, dict):
                b_page = b.get("page", b.get("start_page"))
                b_top = b.get("top", b.get("y", 0))
                b_name = b.get("segment", b.get("name", b.get("section")))
            elif isinstance(b, (list, tuple)) and len(b) >= 2:
                b_page, b_top, b_name = (list(b) + [None, None, None])[:3]
            else:
                continue
            if b_page is None or b_name is None:
                continue
            if page is not None and (b_page < page or (b_page == page and (top is None or b_top is None or b_top <= top))):
                candidates.append((b_page, b_top or 0, b_name))
        if candidates:
            candidates.sort()
            return candidates[-1][2]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pattern", default="*.pdf")
    ap.add_argument("--target-type", default="generic",
                     help="table_type to count, e.g. generic, unknown_continuation, dosing_table")
    ap.add_argument("--target-section", default=None,
                     help="If omitted, count target-type tables across ALL sections")
    ap.add_argument("--show-titles", action="store_true",
                     help="Also print bbox/title for each matching table, not just counts")
    args = ap.parse_args()

    pdf_dir = Path(args.input_dir)
    pdfs = sorted(pdf_dir.glob(args.pattern))
    if args.limit:
        pdfs = pdfs[: args.limit]

    print(f"Found {len(pdfs)} PDFs in {pdf_dir}\n", file=sys.stderr)

    doc_counts = Counter()
    section_counts = Counter()
    doc_examples = {}   # stem -> list of (page, bbox, title, section)
    total = 0
    errors = []

    for i, pdf in enumerate(pdfs):
        if i % 50 == 0:
            print(f"  ...processing {i}/{len(pdfs)}", file=sys.stderr)
        try:
            with pdfplumber.open(str(pdf)) as _pdf:
                _page_count = len(_pdf.pages)
                _para_cache = {}
                for _pg in range(1, _page_count + 1):
                    _lines = _extract_para_lines(_pdf, _pg)
                    for _l in _lines:
                        _l['_page'] = _pg
                    _para_cache[_pg] = [
                        _l for _l in _lines
                        if not any(p.match(_l['text'].strip()) for p in FOOTER_PATTERNS)
                    ]

                table_info,table_bboxes,_spurious_bboxes = find_table_info(_pdf, para_cache=_para_cache)
                raw_lines = _extract_raw_lines_ext(_pdf, table_bboxes)
                meta = _extract_meta(raw_lines, pdf)

                _prefix_result = _is_mm_family_by_prefix(pdf.stem)
                if _prefix_result is not None:
                    _is_mm_family = _prefix_result
                else:
                    _is_mm_family = meta.get('policy_type') in ('medical_policy', 'administrative_policy')

                if _is_mm_family and _is_cpg_family(pdf.stem):
                    _section_vocab = CPG_SECTION_VOCAB
                elif _is_mm_family:
                    _section_vocab = MM_SECTION_VOCAB
                elif _is_ph_family(pdf.stem):
                    _section_vocab = PH_SECTION_VOCAB
                else:
                    _section_vocab = SECTION_VOCAB

                if _is_mm_family:
                    for _pg in range(1, _page_count + 1):
                        _page = _pdf.pages[_pg - 1]
                        for _l in _para_cache.get(_pg, []):
                            _l['underline'] = _has_underline_rect(_page, _l)
                        for _l in raw_lines:
                            if _l['page'] == _pg:
                                _l['underline'] = _has_underline_rect(_page, _l)

                # Parse TOC and Related Coverage Resources if present
                _toc_node, _rcr_node = _parse_toc_rcr(_pdf, table_bboxes)
                _has_toc = bool(_toc_node.entries)

                _section_boundaries = _find_section_boundaries(raw_lines, _section_vocab, _pdf)
                _classify_table_types(table_info, _section_boundaries,
                               table_bboxes=table_bboxes,
                               spurious_narrow_rule_bboxes=_spurious_bboxes,
                               policy_id=pdf.stem,
                               raw_lines=raw_lines)

                _rescue_pref_criteria_continuations(table_info, _section_boundaries)
                _rescue_hcpcs_cpt_icd_continuations(table_info, _section_boundaries, table_bboxes)

                _apply_network_adequacy_override(table_info, _section_boundaries, pdf.stem)
                _apply_toc_table_override(table_info, _section_boundaries, pdf.stem)
                _apply_three_step_medications_override(table_info)
                _apply_dosing_table_override(table_info)

                results = table_info
        except Exception as e:
            errors.append((pdf.stem, f"{type(e).__name__}: {e}"))
            continue

        for r in results:
            if r.get("table_type") != args.target_type:
                continue
            section = find_section_for_table(r, _section_boundaries)
            if args.target_section and section != args.target_section:
                continue
            doc_counts[pdf.stem] += 1
            section_counts[section or '(none)'] += 1
            total += 1
            if args.show_titles:
                _tier_labels = r.get('tier_labels') or {}
                _data_row_count = sum(1 for lbl in _tier_labels.values() if lbl == 'data')
                doc_examples.setdefault(pdf.stem, []).append(
                    (r.get('page'), r.get('bbox'), r.get('title', ''), section,
                     r.get('cols'), _data_row_count))

    label_sec = args.target_section or "ALL SECTIONS"
    print(f"\n=== TOTAL {args.target_type}-in-{label_sec}: {total} ===\n")
    print(f"--- Document frequency (descending) ---")
    for stem, count in doc_counts.most_common(60):
        print(f"  {stem:<70} {count}")

    print(f"\n--- Section breakdown (descending) ---")
    for sec, count in section_counts.most_common(30):
        print(f"  {sec:<40} {count}")

    n_docs = len(doc_counts)
    print(f"\n--- Concentration summary ---")
    print(f"  Total distinct documents contributing: {n_docs}")
    top10_sum = sum(c for _, c in doc_counts.most_common(10))
    if total:
        print(f"  Sum of top 10 documents: {top10_sum} ({top10_sum/total*100:.1f}% of total)")

    if args.show_titles:
        print(f"\n--- Per-document examples ---")
        for stem, examples in doc_examples.items():
            print(f"  {stem}:")
            for pg, bbox, title, section, cols, rows in examples:
                print(f"      page={pg} bbox={bbox} section={section} cols={cols} data rows={rows} title={title!r}")

    if errors:
        print(f"\n=== ERRORS ({len(errors)}) ===")
        for stem, err in errors[:20]:
            print(f"  {stem}: {err}")


if __name__ == "__main__":
    main()
