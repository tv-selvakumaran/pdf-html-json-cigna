#!/usr/bin/env python3
"""
Reconciliation script: full table_type x section breakdown across one or
more corpora, plus grand totals per table_type (across all sections) and
per (table_type, section). Used to figure out where a previously-cited
count (e.g. '166 generic tables in Coverage Policy') actually came from --
by comparing it against several plausible metrics computed here:

  - total 'generic' anywhere (any section)
  - total 'generic' in Coverage Policy specifically
  - total 'spurious' anywhere
  - same breakdowns per-corpus and combined

Usage:
    python3 global_type_count.py --input-dir DIR --corpus-label LABEL [--limit N]

Run once per corpus:
    python3 global_type_count.py --input-dir .../medical-administrative/all-policy-documents --corpus-label medical-administrative > /tmp/mm_global.txt 2>&1
    python3 global_type_count.py --input-dir .../drug/all-policy-documents --corpus-label drug > /tmp/drug_global.txt 2>&1

Then diff/sum the two output files' TOTAL PER TABLE_TYPE sections by hand,
or just eyeball each corpus's numbers against 166.
"""

import argparse
import sys
from collections import Counter
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
    ap.add_argument("--corpus-label", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pattern", default="*.pdf")
    args = ap.parse_args()

    pdf_dir = Path(args.input_dir)
    pdfs = sorted(pdf_dir.glob(args.pattern))
    if args.limit:
        pdfs = pdfs[: args.limit]

    print(f"[{args.corpus_label}] Found {len(pdfs)} PDFs in {pdf_dir}\n", file=sys.stderr)

    type_totals = Counter()               # table_type -> count (any section)
    type_section_totals = Counter()       # (table_type, section) -> count
    docs_with_errors = []
    total_tables = 0
    total_docs_processed = 0

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

                results = table_info
        except Exception as e:
            docs_with_errors.append((pdf.stem, f"{type(e).__name__}: {e}"))
            continue

        total_docs_processed += 1
        for r in results:
            total_tables += 1
            ttype = r.get("table_type", "?")
            section = find_section_for_table(r, _section_boundaries)
            type_totals[ttype] += 1
            type_section_totals[(ttype, section)] += 1

    print(f"\n=== [{args.corpus_label}] docs processed: {total_docs_processed} / {len(pdfs)} (errors: {len(docs_with_errors)}) ===")
    print(f"=== [{args.corpus_label}] TOTAL TABLES: {total_tables} ===\n")

    print(f"--- TOTAL PER TABLE_TYPE (any section) ---")
    for ttype, count in type_totals.most_common(60):
        print(f"  {ttype:<25} {count}")

    print(f"\n--- TOTAL PER (TABLE_TYPE, SECTION) ---")
    for (ttype, section), count in type_section_totals.most_common():
        print(f"  {ttype:<20} {str(section):<32} {count}")

    generic_any = type_totals.get("generic", 0)
    generic_coverage = sum(c for (t, s), c in type_section_totals.items()
                           if t == "generic" and s and "coverage" in str(s).lower() and "polic" in str(s).lower())
    spurious_any = type_totals.get("spurious", 0)
    unknown_any = type_totals.get("unknown", 0)
    unknown_cont_any = type_totals.get("unknown_continuation", 0)

    print(f"\n=== RECONCILIATION CALLOUTS [{args.corpus_label}] ===")
    print(f"  generic (any section):              {generic_any}")
    print(f"  generic (Coverage Policy only):      {generic_coverage}")
    print(f"  spurious (any section):              {spurious_any}")
    print(f"  unknown (any section):               {unknown_any}")
    print(f"  unknown_continuation (any section):  {unknown_cont_any}")
    print(f"  generic + unknown (any section):     {generic_any + unknown_any}")
    print(f"  generic + spurious (any section):    {generic_any + spurious_any}")

    if docs_with_errors:
        print(f"\n=== [{args.corpus_label}] ERRORS ({len(docs_with_errors)}) ===")
        for stem, err in docs_with_errors[:20]:
            print(f"  {stem}: {err}")


if __name__ == "__main__":
    main()
