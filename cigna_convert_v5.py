#!/usr/bin/env python3
"""
cigna_convert_v5.py
===================
Orchestrator for the two-pass Cigna PDF → HTML converter.

Pass 1: cigna_parse.parse(pdf_path)  → CignaDoc tree
Pass 2: cigna_emit.emit(doc)         → HTML string

Usage:
    # Single PDF
    python3 cigna_convert_v5.py \
        --input-dir ~/Data/.../all-policy-documents \
        --payer Cigna \
        --policy-id dqm_001_coveragepositioncriteria_ustekinumab_subcutaneous

    # Batch (with corpus CSV)
    python3 cigna_convert_v5.py \
        --input-dir ~/Data/.../all-policy-documents \
        --corpus-csv cigna_corpus_analysis.csv \
        --max-pages 10 \
        [--dry-run]
"""

import argparse
import csv
import logging
import sys
import time
from collections import defaultdict, Counter
from pathlib import Path

from cigna_parse import parse
from cigna_emit  import emit

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

PREFIX_ORDER = ['dqm', 'ip', 'p', 'ph', 'psm', 'st']


def process_pdf(pdf_path: Path, dry_run: bool = False) -> bool:
    """Convert one PDF. Returns True on success."""
    try:
        doc, _ = parse(pdf_path)
        html = emit(doc)

        title     = doc.meta.get('title', pdf_path.stem)
        sec_count = html.count('class="head_stripe"')
        log.info('  %r', title[:70])
        log.info('  sections=%d', sec_count)

        if not dry_run:
            out = pdf_path.parent / (pdf_path.stem + '.htm')
            out.write_text(html, encoding='utf-8')
            log.info('  Written: %s', out)
        return True

    except Exception as e:
        log.error('  ERROR: %s', e)
        import traceback; traceback.print_exc()
        return False


def run_batch(input_dir: Path, corpus_csv: Path,
              max_pages: int, prefix_filter: str | None,
              dry_run: bool, log_dir: Path):

    rows_by_prefix: dict[str, list] = defaultdict(list)
    skipped = []

    with open(corpus_csv, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            n = int(row['n_pages'])
            pfx = row['prefix']
            if n > max_pages:
                skipped.append(row)
                continue
            if prefix_filter and pfx != prefix_filter:
                continue
            rows_by_prefix[pfx].append(row)

    prefixes = [p for p in PREFIX_ORDER if p in rows_by_prefix]
    for p in sorted(rows_by_prefix):
        if p not in prefixes:
            prefixes.append(p)

    total = sum(len(v) for v in rows_by_prefix.values())
    print(f"{'='*65}")
    print(f"Cigna Batch Converter V5  (two-pass)")
    print(f"  input-dir : {input_dir}")
    print(f"  max-pages : {max_pages}   dry-run: {dry_run}")
    print(f"  PDFs to process : {total}")
    print(f"  Skipped (>{max_pages} pages): {len(skipped)}")
    print(f"  Prefixes: {prefixes}")
    print(f"{'='*65}\n")

    log_dir.mkdir(exist_ok=True)
    grand_ok = grand_err = 0
    summaries = []

    for prefix in prefixes:
        pdf_rows = rows_by_prefix[prefix]

        # Per-prefix log file
        pfx_log = logging.getLogger(f'cigna.{prefix}')
        pfx_log.setLevel(logging.INFO)
        pfx_log.handlers.clear()
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')
        fh = logging.FileHandler(log_dir / f'cigna_v5_{prefix}.log', 'w', encoding='utf-8')
        fh.setFormatter(fmt); pfx_log.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt); pfx_log.addHandler(ch)

        pfx_log.info('='*60)
        pfx_log.info('PREFIX: %s  (%d PDFs)', prefix.upper(), len(pdf_rows))
        pfx_log.info('='*60)

        t0 = time.time(); ok = err = 0
        for i, row in enumerate(pdf_rows, 1):
            pdf_path = input_dir / row['filename']
            if not pdf_path.exists():
                pfx_log.error('[%d/%d] NOT FOUND: %s', i, len(pdf_rows), row['filename'])
                err += 1; continue
            pfx_log.info('[%d/%d] %s  (pages=%s)', i, len(pdf_rows),
                         row['filename'], row['n_pages'])
            if process_pdf(pdf_path, dry_run):
                ok += 1
            else:
                err += 1

        elapsed = time.time() - t0
        pfx_log.info('')
        pfx_log.info('PREFIX SUMMARY %s: ok=%d  err=%d  time=%.1fs',
                     prefix.upper(), ok, err, elapsed)
        summaries.append({'prefix': prefix, 'total': len(pdf_rows),
                          'ok': ok, 'err': err, 'elapsed': elapsed})
        grand_ok += ok; grand_err += err

    print(f"\n{'='*65}")
    print(f"GRAND SUMMARY")
    print(f"{'='*65}")
    print(f"{'PREFIX':<8} {'TOTAL':>6} {'OK':>6} {'ERR':>6} {'TIME':>8}")
    print(f"{'-'*8} {'-'*6} {'-'*6} {'-'*6} {'-'*8}")
    for s in summaries:
        print(f"{s['prefix']:<8} {s['total']:>6} {s['ok']:>6} "
              f"{s['err']:>6} {s['elapsed']:>7.1f}s")
    print(f"{'-'*8} {'-'*6} {'-'*6} {'-'*6}")
    print(f"{'TOTAL':<8} {total:>6} {grand_ok:>6} {grand_err:>6}")
    if skipped:
        print(f"\nSkipped (>{max_pages} pages): {len(skipped)}")
        for r in sorted(skipped, key=lambda x: int(x['n_pages']), reverse=True)[:10]:
            print(f"  {r['filename']:<55} pages={r['n_pages']}")
    print(f"\nLogs: {log_dir.resolve()}/cigna_v5_<prefix>.log")
    print(f"{'='*65}")


def main():
    ap = argparse.ArgumentParser(description='Cigna PDF → HTML (V5 two-pass)')
    ap.add_argument('--input-dir',   required=True)
    ap.add_argument('--payer',       default='Cigna')
    ap.add_argument('--policy-id',   default=None, help='Single policy stem')
    ap.add_argument('--corpus-csv',  default=None, help='CSV from cigna_analyze_corpus.py')
    ap.add_argument('--max-pages',   type=int, default=10)
    ap.add_argument('--prefix',      default=None)
    ap.add_argument('--dry-run',     action='store_true')
    ap.add_argument('--log-dir',     default='logs_v5')
    args = ap.parse_args()

    input_dir = Path(args.input_dir).expanduser()
    if not input_dir.exists():
        log.error('input-dir not found: %s', input_dir); sys.exit(1)

    # ── Single PDF mode ───────────────────────────────────────────────────
    if args.policy_id:
        matches = list(input_dir.rglob(f'{args.policy_id}*.pdf'))
        if not matches:
            log.error('No PDF found for policy-id: %s', args.policy_id); sys.exit(1)
        pdf_path = matches[0]
        log.info('Processing: %s', pdf_path.name)
        ok = process_pdf(pdf_path, args.dry_run)
        sys.exit(0 if ok else 1)

    # ── Batch mode ────────────────────────────────────────────────────────
    if not args.corpus_csv:
        log.error('Provide --policy-id for single PDF, or --corpus-csv for batch.')
        sys.exit(1)

    run_batch(
        input_dir     = input_dir,
        corpus_csv    = Path(args.corpus_csv),
        max_pages     = args.max_pages,
        prefix_filter = args.prefix,
        dry_run       = args.dry_run,
        log_dir       = Path(args.log_dir),
    )


if __name__ == '__main__':
    main()
