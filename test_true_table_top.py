#!/usr/bin/env python3
"""
test_true_table_top.py
======================
Verifies that _true_table_top() correctly resolves the visual top of the
open-top-border continuation table on psm_004 page 3.

Run from pdf-json-converter/:
    python3 test_true_table_top.py
"""
import sys
from pathlib import Path
import pdfplumber

sys.path.insert(0, '.')
from cigna_parse_tables import _true_table_top, find_table_info


def main():
    pdf_path = next(
        Path("~/Data/AIAgents/MedicalAIAgent/payer-policy-data"
             "/pdf-data/cigna/drug/all-policy-documents")
        .expanduser()
        .glob("psm_004*.pdf")
    )
    print(f"PDF: {pdf_path.name}\n")

    # ── Part 1: raw geometry check on page 3 ─────────────────────────────
    with pdfplumber.open(str(pdf_path)) as pdf:
        page = pdf.pages[2]   # page 3, 0-indexed
        tables = page.find_tables()

        print("=== Page 3 tables: pdfplumber bbox vs _true_table_top ===")
        for t in tables:
            bbox = t.bbox
            visual_top = _true_table_top(bbox, page.rects)
            changed = " ← CORRECTED" if abs(visual_top - bbox[1]) > 1 else ""
            print(f"  pdfplumber bbox[1] = {bbox[1]:.1f}   "
                  f"_true_table_top = {visual_top:.1f}{changed}")
            print(f"    full bbox = ({bbox[0]:.1f}, {bbox[1]:.1f}, "
                  f"{bbox[2]:.1f}, {bbox[3]:.1f})")

    # ── Part 2: find_table_info continuation detection ────────────────────
    print("\n=== find_table_info results for psm_004 ===")
    tables_info, covered_bboxes = find_table_info(pdf_path)

    for t in tables_info:
        cont_marker = " *** IS_CONTINUATION ***" if t['is_continuation'] else ""
        print(f"  p{t['page']}  type={t['table_type']:<14}  "
              f"bbox[1]={t['bbox'][1]:.1f}  "
              f"covered={t['covered']}{cont_marker}")

    print(f"\n=== covered_bboxes ===")
    for b in sorted(covered_bboxes):
        print(f"  p{b[0]}  top={b[1]:.1f}  bot={b[2]:.1f}")

    # ── Part 3: confirm the p3 continuation is now covered ───────────────
    p3_cont = next(
        (t for t in tables_info
         if t['page'] == 3 and abs(t['bbox'][1] - 213.4) < 2),
        None
    )
    print()
    if p3_cont is None:
        print("FAIL: p3 continuation table (bbox[1]≈213) not found in results")
        sys.exit(1)
    elif p3_cont['is_continuation']:
        print("PASS: p3 table correctly detected as continuation "
              f"(type={p3_cont['table_type']}, covered={p3_cont['covered']})")
    else:
        print(f"FAIL: p3 table still not detected as continuation "
              f"(type={p3_cont['table_type']}, covered={p3_cont['covered']})")
        sys.exit(1)


if __name__ == '__main__':
    main()
