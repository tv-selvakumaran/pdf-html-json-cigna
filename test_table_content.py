#!/usr/bin/env python3
"""
test_table_content.py
=====================
Test that table HTML output contains correct number of rows.

For each PDF:
  - Calls build_table_nodes() to get TableNodes
  - For each node, finds its source table in pdfplumber by page+top
  - Uses gray_bottom to correctly identify header rows
  - Compares HTML data row count vs pdfplumber data row count

Usage:
    python3 test_table_content.py \
        --input-dir <pdf_dir> \
        --corpus-csv cigna_corpus_analysis.csv \
        --max-pages 70 \
        --out /tmp/table_content_report.txt
"""
import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pdfplumber

sys.path.insert(0, '.')
from cigna_parse_tables import build_table_nodes, _is_spurious_table
from extractor import extract_paragraph_lines


def count_html_rows(html: str) -> int:
    """Count data rows in HTML table tbody."""
    if not html:
        return 0
    tbody = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
    if tbody:
        return len(re.findall(r'<tr\b', tbody.group(1)))
    total = len(re.findall(r'<tr\b', html))
    return max(0, total - 1)


def get_source_table(pdf_path: Path, page: int, top: float):
    """Get pdfplumber tdata and gray_bottom for table at (page, top)."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        pg = pdf.pages[page - 1]
        page_bboxes = [t.bbox for t in pg.find_tables()]
        for t in pg.find_tables():
            if abs(t.bbox[1] - top) > 20:
                continue
            tdata = t.extract() or []
            if not tdata:
                continue
            # Get gray_bottom
            gray_bottom = None
            for r in pg.rects:
                c = r.get('non_stroking_color', 0)
                if isinstance(c, (list, tuple)):
                    c = sum(c)/len(c) if c else 0
                if (r.get('fill') and 0.3 <= float(c) <= 0.98 and
                        r['top'] >= t.bbox[1]-2 and r['bottom'] <= t.bbox[3]+2):
                    if gray_bottom is None or r['bottom'] > gray_bottom:
                        gray_bottom = r['bottom']
            # Compute data_start geometrically using row bboxes
            data_start = 1
            if gray_bottom is not None:
                rows = t.rows  # pdfplumber row objects with bbox
                for ri, row in enumerate(rows):
                    if row.bbox[1] >= gray_bottom - 1:
                        data_start = ri
                        break
            return tdata, gray_bottom, t.bbox, data_start
    return None, None, None, None

def count_source_rows(tdata: list, gray_bottom: float = None,
                      data_start: int = None) -> int:
    if not tdata:
        return 0
    if data_start is not None:
        # Use geometrically-computed data_start directly
        return sum(1 for row in tdata[data_start:]
                   if any((c or '').strip() for c in (row or [])))
    if gray_bottom is not None:
        _hdr_words = {'product', 'strength', 'and', 'form', 'retail', 'home',
                      'delivery', 'maximum', 'quantity', 'limit', 'limits',
                      'per', 'days', 'none', '', 'name', 'criteria', 'type',
                      'revision', 'summary', 'changes', 'date', 'non-covered',
                      'non', 'covered'}
        ds = 1
        for ri, row in enumerate(tdata):
            c0 = (row[0] or '').strip().lower() if row else ''
            c0_words = set(c0.split()) if c0 else set()
            if c0_words and not c0_words.issubset(_hdr_words):
                ds = ri
                break
        return sum(1 for row in tdata[ds:]
                   if any((c or '').strip() for c in (row or [])))
    return sum(1 for row in tdata[1:]
               if any((c or '').strip() for c in (row or [])))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input-dir', required=True)
    ap.add_argument('--corpus-csv', required=True)
    ap.add_argument('--max-pages', type=int, default=12)
    ap.add_argument('--out', default='/tmp/table_content_report.txt')
    ap.add_argument('--only-issues', action='store_true')
    args = ap.parse_args()

    input_dir = Path(args.input_dir)
    pdfs = []
    with open(args.corpus_csv) as f:
        for row in csv.DictReader(f):
            try:
                n = int(row['n_pages'])
            except (ValueError, KeyError):
                continue
            if n <= args.max_pages:
                pdf_path = input_dir / row['filename']
                if pdf_path.exists():
                    pdfs.append((row['filename'], n, pdf_path))

    _start = time.time()
    _start_str = datetime.now().strftime('%H:%M:%S')
    print(f"Found {len(pdfs)} PDFs with <= {args.max_pages} pages  [{_start_str}]",
          flush=True)

    issues = []

    with open(args.out, 'w', encoding='utf-8') as out:
        out.write(f"TABLE CONTENT REPORT — {len(pdfs)} PDFs\n")
        out.write("=" * 110 + "\n")
        out.flush()

        for i, (filename, n_pages, pdf_path) in enumerate(sorted(pdfs)):
            if i % 50 == 0:
                _ts = datetime.now().strftime('%H:%M:%S')
                print(f"  {i}/{len(pdfs)} ...  [{_ts}]", flush=True)

            try:
                para_cache = {}
                with pdfplumber.open(str(pdf_path)) as p:
                    n_pg = len(p.pages)
                for pg in range(1, n_pg+1):
                    lines = extract_paragraph_lines(pdf_path, pg)
                    for l in lines: l['_page'] = pg
                    para_cache[pg] = lines
                nodes = build_table_nodes(pdf_path, para_cache=para_cache)
            except Exception as e:
                out.write(f"\n[{filename}] ERROR: {e}\n")
                out.flush()
                continue

            file_issues = []
            for node in nodes:
                # Skip MOA — wrap-around rows make counting complex
                if node.table_type == 'moa':
                    continue

                # Get source table from pdfplumber
                tdata, gray_bottom, bbox, data_start = get_source_table(
                    pdf_path, node.page, node.top)
                if tdata is None:
                    continue

                expected = count_source_rows(tdata, gray_bottom, data_start)
                actual = count_html_rows(node.html)

                if actual < expected - 1:
                    file_issues.append({
                        'page': node.page,
                        'type': node.table_type,
                        'top': node.top,
                        'expected': expected,
                        'actual': actual,
                        'missing': expected - actual,
                        'row0': str([str(c)[:20] if c else None
                                     for c in (tdata[0] if tdata else [])[:4]]),
                    })

            if file_issues or not args.only_issues:
                status = f"ISSUES={len(file_issues)}" if file_issues else "OK"
                out.write(f"\n{'─'*110}\n")
                out.write(f"FILE: {filename}  pages={n_pages}  "
                          f"tables={len(nodes)}  {status}\n")
                for issue in file_issues:
                    out.write(f"  p{issue['page']:2d} [{issue['type']:<10}]  "
                              f"top={issue['top']:.0f}  "
                              f"expected={issue['expected']}  "
                              f"html={issue['actual']}  "
                              f"MISSING={issue['missing']}\n")
                    out.write(f"             row0: {issue['row0']}\n")
                out.flush()

            if file_issues:
                issues.append((filename, file_issues))

        out.write(f"\n{'='*110}\n")
        out.write(f"SUMMARY: {len(issues)} PDFs with content issues:\n")
        for fn, fi in issues:
            total = sum(x['missing'] for x in fi)
            out.write(f"  {fn}  ({len(fi)} tables, {total} missing rows)\n")

    _elapsed = time.time() - _start
    _end_str = datetime.now().strftime('%H:%M:%S')
    print(f"Done. Report: {args.out}  [{_end_str}  elapsed={_elapsed:.0f}s]")
    print(f"PDFs with content issues: {len(issues)}")


if __name__ == '__main__':
    main()
