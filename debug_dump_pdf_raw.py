#!/usr/bin/env python3
"""Show ALL words from <argv[1]> page <argv[2]> including those dropped by extract_raw_lines."""
import sys, re
from collections import defaultdict
from pathlib import Path
import pdfplumber

pdf_path = Path(sys.argv[1]).expanduser()

with pdfplumber.open(str(pdf_path)) as pdf:
    page = pdf.pages[ int(sys.argv[2]) ]
    words = page.extract_words(
        extra_attrs=['fontname','size'],
        keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
    
    buckets = defaultdict(list)
    for w in words:
        yk = round(w['top']/4)*4
        buckets[yk].append(w)
    
    print(f"{'y':>7} {'x':>7} {'sz':>5} {'fontname':<30} | text[:200]")
    print("-"*100)
    for yk in sorted(buckets):
        ws = sorted(buckets[yk], key=lambda w: w['x0'])
        dom = max(ws, key=lambda w: len(w['text']))
        text = ' '.join(w['text'] for w in ws)[:200]
        dropped = dom['size'] < 8.0
        print(f"{yk:7.1f} {ws[0]['x0']:7.1f} {dom['size']:5.1f} "
              f"{'DROP' if dropped else '    '} "
              f"{dom['fontname'][:30]:<30} | {text}")
