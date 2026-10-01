#!/usr/bin/env python3
"""
For a single specified PDF, dump every 'generic'-type table_info entry's
full tdata, PLUS the fill color of any rects overlapping the table's top
~20pt (candidate header-shading rects) and any rects overlapping OTHER
rows further down (candidate sub-header shading, mid-table) -- to confirm
the gray-background-header hypothesis with real data before writing any
detector.

Usage:
    python3 dump_document_generic_tables.py --pdf-path /path/to/file.pdf [--target-type generic]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, ".")

import pdfplumber

from cigna_parse import _extract_meta, _parse_toc_rcr
from cigna_constants import (
    FOOTER_PATTERNS, 
    MM_SECTION_VOCAB, SECTION_VOCAB, 
    CPG_SECTION_VOCAB, PH_SECTION_VOCAB,
    )
from cigna_parse_tables import (
    find_table_info, _classify_table_types, 
    _rescue_pref_criteria_continuations, 
    _rescue_hcpcs_cpt_icd_continuations,
    _apply_network_adequacy_override, 
    _apply_dosing_table_override,
    _apply_three_step_medications_override,
    _apply_toc_table_override,
    _is_mm_family_by_prefix, 
    _is_cpg_family, _is_ph_family,
)
from cigna_extractor import (
    extract_raw_lines as _extract_raw_lines_ext,
    extract_paragraph_lines as _extract_para_lines,
    _find_section_boundaries,
    _has_underline_rect,
)


def find_section_for_table(table_info, section_boundaries, debug=False):
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
        if debug:
            print(f"    [DEBUG find_section_for_table] page={page!r} top={top!r} candidates={candidates}")
        if candidates:
            candidates.sort()
            return candidates[-1][2]
    return None


def describe_fill_rects_in_range(page, top, bottom):
    found = []
    for r in page.rects:
        if not r.get('fill'):
            continue
        if r['bottom'] < top or r['top'] > bottom:
            continue
        fc = r.get('non_stroking_color', None)
        found.append((fc, round(r['top'], 1), round(r['bottom'], 1), round(r['x0'], 1), round(r['x1'], 1)))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-path", required=True)
    ap.add_argument("--target-type", default="generic")
    ap.add_argument("--max-tables", type=int, default=10)
    args = ap.parse_args()

    pdf = Path(args.pdf_path).expanduser()

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

        table_info, table_bboxes, _spurious_bboxes = find_table_info(_pdf, para_cache=_para_cache)
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

        _toc_node, _rcr_node = _parse_toc_rcr(_pdf, table_bboxes)
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

        shown = 0
        for entry in table_info:
            if args.target_type and entry.get("table_type") != args.target_type:
                continue
            if shown >= args.max_tables:
                break
            shown += 1

            pg = entry['page']
            bbox = entry['bbox']
            page_obj = _pdf.pages[pg - 1]
            section = find_section_for_table(entry, _section_boundaries, debug=False)

            # print(f"\n{'='*90}")
            print(f"page={pg} bbox={bbox} table_type={entry.get('table_type')!r} section={section} title={entry.get('title')!r}, is_cont: {entry.get('is_continuation')}")
            print(f"--- tdata ---")
            for row in entry.get('tdata', []):
                print(f"    {row}")
            print(f"--- tier_labels ---")
            print(f"    {entry.get('tier_labels')}")
            print(f"--- footnote --")
            print(f"    {entry.get('footnote')}")
            # print(f"--- consolidated_tdata ---")
            # for row in entry.get('consolidated_tdata', []):
            #     print(f"    {row}")
            print(f"--- matrix_tier_labels ---")
            print(f"    {entry.get('matrix_tier_labels')}")
            print(f"--- matrix_consolidated_tdata ---")
            for row in (entry.get('matrix_consolidated_tdata') or []):
                print(f"    {row}")

if __name__ == "__main__":
    main()
