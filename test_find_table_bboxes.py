#!/usr/bin/env python3
"""
test_find_table_bboxes.py
=========================
Test table detection by calling parse() for each PDF and comparing
the table_bboxes it returns against pdfplumber's raw table detection.

Uses section context from the CignaDoc tree to correctly classify
References-section tables as SPURIOUS rather than MISSED.
"""
import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import pdfplumber

sys.path.insert(0, '.')
from cigna_parse import parse
from cigna_parse_nodes import SectionNode
from cigna_parse_tables import _is_spurious_table, find_table_info


def get_references_range(doc) -> tuple:
    """Return ((start_page, start_y), (end_page, end_y)) for References section.
    end is the start of the next section, or None if References is last."""
    sections = [n for n in doc.nodes if isinstance(n, SectionNode)]
    for i, node in enumerate(sections):
        if 'reference' in node.heading.lower():
            start = (node.page, node.top)
            end = (sections[i+1].page, sections[i+1].top) if i+1 < len(sections) else None
            return start, end
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input-dir', required=True)
    ap.add_argument('--corpus-csv', required=True)
    ap.add_argument('--max-pages', type=int, default=12)
    ap.add_argument('--policy-id', default='')
    ap.add_argument('--out', default='/tmp/table_bboxes_report.txt')
    args = ap.parse_args()

    input_dir = Path(args.input_dir)

    pdfs = []
    with open(args.corpus_csv) as f:
        for row in csv.DictReader(f):
            try:
                n = int(row['n_pages'])
            except (ValueError, KeyError):
                continue
            if args.policy_id and args.policy_id not in row['filename']:
                continue
            if n <= args.max_pages:
                pdf_path = input_dir / row['filename']
                if pdf_path.exists():
                    pdfs.append((row['filename'], n, pdf_path))

    _start = time.time()
    _start_str = datetime.now().strftime("%H:%M:%S")
    print(f"Found {len(pdfs)} PDFs with <= {args.max_pages} pages  [{_start_str}]", flush=True)

    total_missed = []

    with open(args.out, 'w', encoding='utf-8') as out:
        out.write(f"TABLE BBOX REPORT — {len(pdfs)} PDFs, max_pages={args.max_pages}\n")
        out.write("=" * 110 + "\n")
        out.flush()

        for i, (filename, n_pages, pdf_path) in enumerate(sorted(pdfs)):
            if i % 50 == 0:
                _ts = datetime.now().strftime("%H:%M:%S")
                print(f"  {i}/{len(pdfs)} ...  [{_ts}]", flush=True)

            try:
                doc, our_bboxes = parse(pdf_path)
            except Exception as e:
                out.write(f"\n[{filename}] ERROR in parse: {e}\n")
                out.flush()
                continue

            # Get table type info from find_table_info
            _fi_tables, _ = find_table_info(pdf_path)
            _type_map = {(t['page'], t['bbox']): t['table_type']
                         for t in _fi_tables}

            # Get References section range from doc tree
            ref_start, ref_end = get_references_range(doc)

            def in_references(pg: int, bbox_top: float) -> bool:
                if ref_start is None:
                    return False
                ref_pg, ref_y = ref_start
                after_start = pg > ref_pg or (pg == ref_pg and bbox_top > ref_y)
                if not after_start:
                    return False
                if ref_end is None:
                    return True
                end_pg, end_y = ref_end
                before_end = pg < end_pg or (pg == end_pg and bbox_top < end_y)
                return before_end

            def is_covered(pg, bbox):
                for b in our_bboxes:
                    if b[0] == pg and b[1] <= bbox[1] + 10 and b[2] >= bbox[3] - 10:
                        return True
                return False

            # Get all pdfplumber tables
            try:
                all_pdf_tables = []
                with pdfplumber.open(str(pdf_path)) as pdf:
                    for pg, page in enumerate(pdf.pages, 1):
                        page_bboxes = [t.bbox for t in page.find_tables()]
                        words = page.extract_words(extra_attrs=['fontname', 'size'])
                        for t in page.find_tables():
                            tdata = t.extract() or []
                            row0 = tdata[0] if tdata else []
                            row0_str = str([str(c)[:20] if c else None
                                           for c in row0[:5]])
                            t_top = t.bbox[1]
                            title_ws = [w for w in words
                                        if t_top - 16 <= w['top'] < t_top
                                        and 'Bold' in w.get('fontname', '')]
                            title = ' '.join(w['text'] for w in title_ws)[:80]
                            _bbox_key = (pg, tuple(round(x,1) for x in t.bbox))
                            all_pdf_tables.append({
                                'page': pg,
                                'bbox': tuple(round(x, 1) for x in t.bbox),
                                'rows': len(tdata),
                                'cols': len(row0),
                                'row0': row0_str,
                                'title': title,
                                'tdata': tdata,
                                'page_bboxes': page_bboxes,
                                'table_type': _type_map.get(
                                    (pg, tuple(round(x,1) for x in t.bbox)),
                                    'unknown'),
                                'page_obj': page,   # ← add this  
                            })
            except Exception as e:
                out.write(f"\n[{filename}] ERROR reading tables: {e}\n")
                out.flush()
                continue

            # Classify each table
            spurious_tables = []
            real_missed = []
            real_ok = []

            for t in all_pdf_tables:
                bbox = t['bbox']
                pg = t['page']
                # Check spurious first
                if _is_spurious_table(
                        t['tdata'], bbox,
                        t['page_bboxes'],
                        page=t['page_obj']):
                    spurious_tables.append(t)
                elif in_references(pg, bbox[1]):
                    spurious_tables.append(t)  # references citations
                elif is_covered(pg, bbox):
                    real_ok.append(t)
                else:
                    real_missed.append(t)

            real_tables = real_ok + real_missed
            spur_str = f"  spurious={len(spurious_tables)}" if spurious_tables else ""
            status = f"MISSED={len(real_missed)}" if real_missed else "OK"

            out.write(f"\n{'─'*110}\n")
            out.write(f"FILE: {filename}  pages={n_pages}  "
                      f"real_tables={len(real_tables)}{spur_str}  "
                      f"our_bboxes={len(our_bboxes)}  {status}\n")

            out.write(f"  OUR BBOXES (page, top, bot):\n")
            if our_bboxes:
                for b in sorted(our_bboxes):
                    out.write(f"    p{b[0]:2d}  top={b[1]:7.1f}  bot={b[2]:7.1f}\n")
            else:
                out.write(f"    (none)\n")

            out.write(f"  ALL REAL TABLES:\n")
            if real_tables:
                for t in sorted(real_tables, key=lambda x: (x['page'], x['bbox'][1])):
                    mark = 'OK    ' if t in real_ok else 'MISSED'
                    out.write(f"    p{t['page']:2d} [{mark}]  "
                              f"type={t.get('table_type', 'unknown'):<12}  "
                              f"bbox=({t['bbox'][0]},{t['bbox'][1]:.1f},"
                              f"{t['bbox'][2]},{t['bbox'][3]:.1f})  "
                              f"rows={t['rows']}  cols={t['cols']}\n")
                    out.write(f"             title: '{t['title']}'\n")
                    out.write(f"             row0:  {t['row0']}\n")
            else:
                out.write(f"    (none)\n")

            if spurious_tables:
                out.write(f"  SPURIOUS TABLES:\n")
                for t in spurious_tables:
                    out.write(f"    p{t['page']:2d} [SPURIOUS]  "
                              f"bbox=({t['bbox'][0]},{t['bbox'][1]:.1f},"
                              f"{t['bbox'][2]},{t['bbox'][3]:.1f})  "
                              f"rows={t['rows']}  cols={t['cols']}\n")
                    out.write(f"             row0:  {t['row0']}\n")

            out.flush()
            if real_missed:
                total_missed.append(filename)

        out.write(f"\n{'='*110}\n")
        out.write(f"SUMMARY: {len(total_missed)} PDFs have missed real tables:\n")
        for s in sorted(total_missed):
            out.write(f"  {s}\n")

    _elapsed = time.time() - _start
    _end_str = datetime.now().strftime("%H:%M:%S")
    print(f"Done. Report: {args.out}  [{_end_str}  elapsed={_elapsed:.0f}s]")
    print(f"PDFs with missed real tables: {len(total_missed)}")


if __name__ == '__main__':
    main()
