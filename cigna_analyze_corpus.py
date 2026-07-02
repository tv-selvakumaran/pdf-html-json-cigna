#!/usr/bin/env python3
"""
cigna_analyze_corpus.py
=======================
Analyzes Cigna drug and medical-administrative PDFs and produces:
  1. Section structure for each PDF
  2. Groups PDFs by their section pattern
  3. Table type flags for every table type handled by the pipeline

CSV columns:
  filename, prefix, n_pages, sections,
  has_avail, has_dql, has_revision, has_fda_dosing, has_moa,
  has_known_kw, has_titled_generic, has_criteria, has_hcpcs,
  has_eua, has_fda_rx, has_nonpref, has_drug_equiv,
  has_medication_moa, has_condition_criteria, has_catch_all

Usage:
    python3 cigna_analyze_corpus.py \
        --input-dir /path/to/pdf/directory \
        [--output-csv cigna_corpus_analysis.csv]
"""

import argparse
import csv
import re
from collections import defaultdict, Counter
from pathlib import Path

import pdfplumber

# ── Section heading vocabulary ────────────────────────────────────────────────
SECTION_HEADINGS = {
    'coverage policy', 'references', 'revision details',
    'coding information', 'overview', 'general information',
    'background', 'appendix', 'definitions', 'instructions for use',
    'conditions not covered', 'product criteria', 'guidelines',
    'other uses with supportive evidence', 'disease overview',
    'safety', 'recommendations', 'medical necessity criteria',
    'health equity considerations', 'general background',
    'medicare coverage determinations', 'scope', 'procedure',
    'standard procedure', 'attachments', 'compliance measure',
    'state/federal guidelines', 'state/federal compliance',
}

# ── Known-keyword table column headers ───────────────────────────────────────
KNOWN_KW = {'Compound Name', 'Drug Name', 'Comments',
            'Prescribing Information', 'Ingredient'}

# ── Titled-generic table title keywords ──────────────────────────────────────
TITLED_GENERIC_KW = (
    'preferred and non-preferred', 'preferred products', 'by indication',
    'drug availability', 'dosage forms', 'fda approved', 'fda recommended',
    'simon broome', 'dutch lipid', 'laboratory diagnosis',
    'diagnostic criteria', 'reauthorization criteria', 'dose conversion',
    'dosing regimen', 'indications', 'individual and family plans',
    'employer plans', 'fda approved indication', 'fda recommended dosing',
    'fda approved products',
)
TITLED_GENERIC_EXACT = {
    'dosing', 'drug availability', 'dosage forms for this indication',
    'follistim pen dose conversion table*',
}


def _flat(rows, n=3):
    """Flatten first n rows of tdata to a single string."""
    return ' '.join(
        (c or '').strip()
        for row in (rows[:n] or [])
        for c in (row or [])
        if (c or '').strip()
    )


def _title_above(page, table_top, size_tolerance=1.0):
    """Extract bold title text from lines immediately above a table."""
    words = page.extract_words(extra_attrs=['fontname', 'size'])
    candidates = [w for w in words
                  if w['top'] < table_top and
                  table_top - w['top'] <= 20 and
                  'Bold' in w.get('fontname', '')]
    if not candidates:
        return ''
    # Anchor on size of closest line, reject lines with very different size
    candidates.sort(key=lambda w: w['top'], reverse=True)
    anchor_size = candidates[0]['size']
    filtered = [w for w in candidates
                if abs(w['size'] - anchor_size) <= size_tolerance]
    filtered.sort(key=lambda w: (w['top'], w['x0']))
    raw = ' '.join(w['text'] for w in filtered)
    # Collapse letter-spaced headings (e.g. 'A PPENDIX' → 'APPENDIX')
    norm = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', raw)
    return norm.strip()


def detect_tables(pdf_path: Path) -> dict:
    """
    Scan all pages of a PDF and return a dict of table-type boolean flags
    plus section list and page count.
    """
    flags = {
        'n_pages':               0,
        'sections':              [],
        'has_avail':             False,
        'has_dql':               False,
        'has_revision':          False,
        'has_fda_dosing':        False,
        'has_moa':               False,
        'has_known_kw':          False,
        'has_titled_generic':    False,
        'has_criteria':          False,
        'has_hcpcs':             False,
        'has_eua':               False,
        'has_fda_rx':            False,
        'has_nonpref':           False,
        'has_drug_equiv':        False,
        'has_medication_moa':    False,
        'has_condition_criteria':False,
        'has_catch_all':         False,
    }

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            flags['n_pages'] = len(pdf.pages)

            for page in pdf.pages:
                # ── Section headings ──────────────────────────────────────
                words = page.extract_words(
                    extra_attrs=['fontname', 'size'],
                    x_tolerance=3, y_tolerance=3)
                buckets = defaultdict(list)
                for w in words:
                    yk = round(w['top'] / 4) * 4
                    buckets[yk].append(w)

                for yk in sorted(buckets):
                    ws = sorted(buckets[yk], key=lambda w: w['x0'])
                    text = ' '.join(w['text'] for w in ws).strip()
                    bold = any('Bold' in w['fontname'] for w in ws)
                    size = max(w['size'] for w in ws)
                    tl = text.lower().strip()
                    # Collapse letter-spacing
                    tl_norm = re.sub(r'(?<=[a-z]) (?=[a-z])', '', tl)
                    if bold and size >= 9.0:
                        if tl in SECTION_HEADINGS or tl_norm in SECTION_HEADINGS:
                            flags['sections'].append(text)
                        elif tl.startswith('coverage policy'):
                            flags['sections'].append('Coverage Policy')

                # ── Availability: rotated tiny-font column headers ─────────
                if not flags['has_avail']:
                    tiny_by_x = defaultdict(list)
                    tiny_words = page.extract_words(
                        extra_attrs=['size'], x_tolerance=2, y_tolerance=2)
                    for w in tiny_words:
                        if w['size'] < 8.0 and len(w['text']) <= 3:
                            xk = round(w['x0'] / 8) * 8
                            tiny_by_x[xk].append(w)
                    if sum(1 for ws in tiny_by_x.values() if len(ws) >= 4) >= 4:
                        flags['has_avail'] = True

                # ── Per-table detection ───────────────────────────────────
                for t in page.find_tables():
                    tdata = t.extract()
                    if not tdata or len(tdata) < 1:
                        continue

                    row0 = tdata[0] or []
                    first = (row0[0] or '').strip()
                    flat_r0 = ' '.join(str(c) for c in row0 if c)
                    flat_top3 = _flat(tdata)
                    title = _title_above(page, t.bbox[1])
                    title_lc = title.lower()

                    # Revision Details
                    if first == 'Type of Revision':
                        flags['has_revision'] = True
                        continue

                    # DQL — Product / Strength / Retail / Home Delivery
                    if first == 'Product':
                        dql_kw = {
                            'strength', 'retail', 'home', 'delivery',
                            'maximum', 'quantity', 'limit', 'days', 'supply',
                        }
                        if dql_kw & set(flat_r0.lower().split()):
                            flags['has_dql'] = True
                            continue

                    # FDA-Approved Dosing — title has 'fda'+'dosing', None in col0
                    if ('fda' in title_lc and 'dosing' in title_lc and
                            any(row[0] is None for row in tdata[:3])):
                        flags['has_fda_dosing'] = True
                        continue

                    # Mechanism of Action appendix
                    if ('Mechanism of Action' in flat_top3 and
                            (row0[0] or '').strip() == ''):
                        flags['has_moa'] = True
                        continue

                    # Known-keyword tables
                    if any(kw in flat_r0 for kw in KNOWN_KW):
                        flags['has_known_kw'] = True
                        continue

                    # Titled generic — title matches known keywords or Table N.
                    t_norm = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', title)
                    t_norm_lc = t_norm.lower()
                    is_titled = (
                        re.match(r'(Appendix\s+)?Table\s+\d+[.]', t_norm) or
                        re.match(r'Table\s+\d+[:\s]', t_norm) or
                        any(kw in t_norm_lc for kw in TITLED_GENERIC_KW) or
                        t_norm_lc.strip() in TITLED_GENERIC_EXACT
                    )
                    if is_titled:
                        flags['has_titled_generic'] = True
                        continue

                    # Criteria / Coverage criteria
                    if (('Criteria' in flat_r0 and
                            ('Indication' in flat_r0 or
                             'Coverage' in flat_r0 or
                             'Criteria for Use' in flat_r0)) or
                            'Coverage Criteria' in flat_r0):
                        flags['has_criteria'] = True
                        continue

                    # HCPCS / CPT / ICD-10
                    if any(kw in flat_r0 for kw in ('HCPCS', 'CPT', 'ICD-10')):
                        flags['has_hcpcs'] = True
                        continue

                    # EUA Letter
                    if 'Letter of Medical Necessity' in flat_top3:
                        flags['has_eua'] = True
                        continue

                    # FDA Prescribing Information
                    if ('FDA' in flat_r0 and
                            'Prescribing Information' in flat_top3):
                        flags['has_fda_rx'] = True
                        continue

                    # Non-Preferred / Exception Criteria
                    if ('Non-Preferred' in flat_r0 or
                            'Exception' in flat_r0):
                        flags['has_nonpref'] = True
                        continue

                    # Drug Equivalent (Non-Covered Brand / Bioequivalent)
                    if ('Non-Covered Brand' in flat_r0 or
                            'Bioequivalent' in flat_r0):
                        flags['has_drug_equiv'] = True
                        continue

                    # Medication / Mode of Administration
                    if ('Medication' in flat_r0 and
                            'Mode of Administration' in flat_top3):
                        flags['has_medication_moa'] = True
                        continue

                    # Condition / Criteria for Use
                    if ('Condition' in flat_r0 and
                            'Criteria for Use' in flat_top3):
                        flags['has_condition_criteria'] = True
                        continue

                    # Catch-all: any multi-column unclassified table
                    if len(row0) >= 2:
                        flags['has_catch_all'] = True

    except Exception:
        pass

    return flags


def main():
    ap = argparse.ArgumentParser(
        description='Analyze Cigna PDF corpus — all table types')
    ap.add_argument('--input-dir', required=True)
    ap.add_argument('--output-csv', default='cigna_corpus_analysis.csv')
    args = ap.parse_args()

    input_dir = Path(args.input_dir).expanduser()
    pdfs = sorted(input_dir.glob('*.pdf'))
    print(f"Found {len(pdfs)} PDFs in {input_dir}")

    fieldnames = [
        'filename', 'prefix', 'n_pages', 'sections',
        'has_avail', 'has_dql', 'has_revision', 'has_fda_dosing',
        'has_moa', 'has_known_kw', 'has_titled_generic',
        'has_criteria', 'has_hcpcs', 'has_eua', 'has_fda_rx',
        'has_nonpref', 'has_drug_equiv', 'has_medication_moa',
        'has_condition_criteria', 'has_catch_all',
    ]

    rows = []
    pattern_counter = Counter()
    table_type_counter = Counter()

    for i, pdf_path in enumerate(pdfs, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(pdfs)} ...", flush=True)

        prefix = pdf_path.name.split('_')[0]
        flags = detect_tables(pdf_path)

        # Deduplicate consecutive identical sections
        deduped = []
        for s in flags['sections']:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        pattern = ' → '.join(deduped) if deduped else '(none)'

        row = {
            'filename': pdf_path.name,
            'prefix':   prefix,
            'n_pages':  flags['n_pages'],
            'sections': pattern,
        }
        for f in fieldnames[4:]:
            row[f] = flags[f]
            if flags[f]:
                table_type_counter[f] += 1

        rows.append(row)
        pattern_counter[pattern] += 1

    # ── Write CSV ─────────────────────────────────────────────────────────
    out_path = Path(args.output_csv)
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV written: {out_path} ({len(rows)} rows)")

    # ── Table type distribution ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"TABLE TYPE DISTRIBUTION ({len(pdfs)} PDFs)")
    print(f"{'='*60}")
    for flag, count in sorted(table_type_counter.items(),
                               key=lambda x: -x[1]):
        pct = 100 * count / len(pdfs)
        print(f"  {flag:<30} {count:4d}  ({pct:5.1f}%)")

    # ── Top section patterns ──────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"TOP SECTION PATTERNS")
    print(f"{'='*60}")
    for pattern, count in pattern_counter.most_common(15):
        prefixes = Counter(r['prefix'] for r in rows
                           if r['sections'] == pattern)
        print(f"\n  [{count:3d}x] {pattern}")
        print(f"         prefixes: {dict(prefixes)}")

    print(f"\nDone.")


if __name__ == '__main__':
    main()
