#!/usr/bin/env python3
"""
extractor.py
============
Centralised PDF line extraction for the Cigna V5 two-pass converter.

Two public functions:

    extract_raw_lines(pdf_path, table_bboxes)
        ── Called by cigna_parse.py
        ── Focuses on structural boundaries: section/subsection headings,
           table regions, IFU boilerplate, page headers/footers
        ── Output: list[dict] with keys:
               page, top, x0, text, size, fontname, bold, italic, in_table

    extract_paragraph_lines(pdf_path, page_num, y_start, y_end)
        ── Called by reconstruct_cigna_bullet.py (and test scripts)
        ── Focuses on paragraph content: bullet markers, numbered items,
           notes, plain text — within a single paragraph's y-range
        ── Output: list[dict] with keys:
               top, x0, text, size, fontname, bold, italic
               + optional  marker_type: 'bullet'|'sub_bullet'|'sub_sub_bullet'
"""

from __future__ import annotations
import re
from collections import defaultdict
from pathlib import Path

import pdfplumber


# ════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ════════════════════════════════════════════════════════════════════════════

def _is_bold(f: str) -> bool:
    return 'Bold' in f or 'bold' in f

def _is_italic(f: str) -> bool:
    return 'Italic' in f or 'italic' in f or 'Oblique' in f

def _clean(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '–').replace('\u2014', '—')
    text = text.replace('\u00a0', ' ')
    return text


# ════════════════════════════════════════════════════════════════════════════
# Known bullet glyphs (shared between both extraction paths)
# ════════════════════════════════════════════════════════════════════════════

BULLET_CHARS = frozenset({
    '•', '●', '○', '◦',
    '\u2022',   # BULLET
    '\u2023',   # TRIANGULAR BULLET
    '\uf0b7',   # Windows Symbol bullet (Private Use Area)
    '\uf0a7',   # Windows Symbol small bullet
})


# ════════════════════════════════════════════════════════════════════════════
# extract_raw_lines  —  for cigna_parse.py
# ════════════════════════════════════════════════════════════════════════════

FOOTER_PATTERNS = [
    re.compile(r'^Page\s+\d+\s+of\s+\d+', re.I),
    re.compile(r'^Coverage\s+Policy\s+Number\s*:', re.I),
]

SECTION_VOCAB = {
    'overview', 'coverage policy', 'references', 'revision details',
    'coding information', 'general information', 'background',
    'appendix', 'definitions', 'instructions for use',
    'medical necessity criteria', 'reauthorization criteria',
    'authorization duration', 'conditions not covered', 
    'product criteria', 'other uses with supportive evidence',
    'disease overview', 'guidelines', 'safety', 'recommendations',
}

def _normalize_heading(text: str) -> str:
    """Remove letter-spacing: 'O VERVIEW' → 'overview'."""
    t = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', text)
    return t.lower().strip()


def extract_raw_lines(pdf_path: Path, table_bboxes: set) -> list[dict]:
    """
    Extract all lines from the full PDF for structural parsing.

    Merges superscript digits inline, skips footer lines.
    Merges split single-char section headings ('O' + 'VERVIEW' → 'Overview').

    Args:
        pdf_path:     Path to PDF file.
        table_bboxes: set of (page_num, top, bottom) from _find_table_bboxes().

    Returns:
        list of line dicts:
            page, top, x0, text, size, fontname, bold, italic, in_table
    """
    lines = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            words = page.extract_words(
                extra_attrs=['fontname', 'size'],
                keep_blank_chars=False,
                x_tolerance=3, y_tolerance=3,
            )
            if not words:
                continue

            buckets: dict[int, list] = {}
            for w in words:
                yk = round(w['top'] / 4) * 4
                buckets.setdefault(yk, []).append(w)

            for yk in sorted(buckets):
                ws = sorted(buckets[yk], key=lambda w: w['x0'])
                # Merge superscripts (size < 8) into preceding token
                tokens = []
                for w in ws:
                    if w['size'] < 8.0:
                        if tokens:
                            tokens[-1] = tokens[-1] + w['text']
                    else:
                        tokens.append(w['text'])
                text = _clean(' '.join(tokens))
                if not text:
                    continue

                # Skip page footers
                if yk > 715 or any(p.match(text) for p in FOOTER_PATTERNS):
                    continue

                dominant = max(
                    (w for w in ws if w['size'] >= 8.0),
                    key=lambda w: len(w['text']),
                    default=ws[0],
                )
                lines.append({
                    'page':     page_num,
                    'top':      yk,
                    'x0':       ws[0]['x0'],
                    'text':     text,
                    'size':     dominant['size'],
                    'fontname': dominant['fontname'],
                    'bold':     _is_bold(dominant['fontname']),
                    'italic':   _is_italic(dominant['fontname']),
                    'in_table': any(
                        pg == page_num and top <= yk <= bot
                        for pg, top, bot in table_bboxes
                    ),
                })

    # ── Merge split section headings: 'O' + 'VERVIEW' → 'Overview' ────────
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (line['bold'] and line['size'] >= 8.0 and
                len(line['text']) <= 3 and line['text'].isupper() and
                i + 1 < len(lines)):
            nxt = lines[i + 1]
            combined = line['text'] + nxt['text']
            norm = _normalize_heading(combined)
            if norm in SECTION_VOCAB and abs(nxt['top'] - line['top']) <= 8:
                merged_line = dict(line)
                merged_line['text'] = combined.capitalize()
                for sv in SECTION_VOCAB:
                    if sv == norm:
                        merged_line['text'] = sv.title()
                        break
                merged_line['size'] = 14.0
                merged.append(merged_line)
                i += 2
                continue
        merged.append(line)
        i += 1

    return merged


# ════════════════════════════════════════════════════════════════════════════
# extract_paragraph_lines  —  for reconstruct_cigna_bullet.py
# ════════════════════════════════════════════════════════════════════════════

def extract_paragraph_lines(pdf_path: Path, page_num: int,
                             y_start: float = 0,
                             y_end:   float = 9999) -> list[dict]:
    """
    Extract lines from one paragraph block within a PDF page.

    Identifies bullet markers (SymbolMT •, CourierNew o, Wingdings □,
    private-use \uf0b7) and tags them with marker_type. Mixed-font buckets
    (marker + text on same y) are split: marker gets marker_type, text is
    preserved in the 'text' field.

    Args:
        pdf_path:  Path to PDF file.
        page_num:  1-based page number.
        y_start:   Top of paragraph block (inclusive). Default 0.
        y_end:     Bottom of paragraph block (inclusive). Default 9999.

    Returns:
        list of line dicts:
            top, x0, text, size, fontname, bold, italic
            + optional  marker_type: 'bullet' | 'sub_bullet' | 'sub_sub_bullet'
    """
    lines = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        page = pdf.pages[page_num - 1]
        words = page.extract_words(
            extra_attrs=['fontname', 'size'],
            keep_blank_chars=False, x_tolerance=3, y_tolerance=3)

        buckets: dict[float, list] = defaultdict(list)
        for w in words:
            top = w['top']
            if not (y_start <= top <= y_end):
                continue
            # Proximity-based bucketing: find existing bucket within ±2pt
            # This keeps superscript symbols (®, ™) on the same line as
            # their adjacent text, regardless of slight y-coordinate differences
            matched_key = None
            for k in buckets:
                if abs(k - top) <= 2.0:
                    matched_key = k
                    break
            yk = matched_key if matched_key is not None else top
            buckets[yk].append(w)

        # ── Pre-pass: merge split single-char headings ───────────────────
        # (e.g. 'O' at y=192 + 'VERVIEW' at y=196 → one bucket)
        sorted_yk = sorted(buckets)
        skip_yk = set()
        for idx, yk in enumerate(sorted_yk):
            ws = buckets[yk]
            valid = [w for w in ws if w['size'] >= 6.0]
            if (len(valid) == 1
                    and len(valid[0]['text'].strip()) == 1
                    and valid[0]['text'].strip().isupper()
                    and 'SymbolMT'  not in valid[0]['fontname']
                    and 'Wingdings' not in valid[0]['fontname']
                    and 'Courier'   not in valid[0]['fontname']):
                for nidx in [idx + 1, idx - 1]:
                    if 0 <= nidx < len(sorted_yk):
                        nyk = sorted_yk[nidx]
                        if abs(nyk - yk) <= 8 and nyk not in skip_yk:
                            buckets[nyk] = valid + buckets[nyk]
                            skip_yk.add(yk)
                            break

        for yk in sorted_yk:
            if yk in skip_yk:
                continue
            ws = sorted(buckets[yk], key=lambda w: w['x0'])
            valid = [w for w in ws if w['size'] >= 6.0]
            if not valid:
                continue

            # ── Classify each word as marker or text ─────────────────────
            marker_type = None
            text_words  = []
            for w in valid:
                fn  = w['fontname']
                txt = w['text'].strip()

                if 'Wingdings' in fn:
                    marker_type = 'sub_sub_bullet'

                elif 'Courier' in fn and txt == 'o':
                    marker_type = 'sub_bullet'

                elif ('SymbolMT' in fn and
                      (txt in BULLET_CHARS or
                       any(ord(c) in (0xf0b7, 0xf0a7, 0x2022, 0x2023)
                           for c in txt))):
                    marker_type = 'bullet'

                elif txt.startswith('•') or txt.startswith('\u2022'):
                    marker_type = 'bullet'
                    stripped = txt.lstrip('•\u2022 ')
                    if stripped:
                        text_words.append({
                            'text': stripped,
                            'x0':  w['x0'] + 10,
                            'fontname': fn,
                            'size': w['size'],
                        })
                else:
                    text_words.append(w)

            text    = re.sub(r'\s+', ' ',
                             ' '.join(w['text'] for w in text_words)).strip()
            text_x0 = (text_words[0]['x0'] if text_words
                       else (valid[1]['x0'] if len(valid) > 1
                             else valid[0]['x0']))
            dom     = (max(text_words, key=lambda w: len(w['text']))
                       if text_words else valid[0])

            if not text and not marker_type:
                continue

            line_dict: dict = {
                'top':      yk,
                'x0':       text_x0,
                'text':     text,
                'size':     dom['size'],
                'fontname': dom['fontname'],
                'bold':     _is_bold(dom['fontname']),
                'italic':   _is_italic(dom['fontname']),
            }
            if marker_type:
                line_dict['marker_type'] = marker_type
            lines.append(line_dict)

    return lines


def _has_underline_rect(page, line: dict) -> bool:
    line_top = line['top']
    line_bot = line['top'] + line['size']
    line_x0  = line['x0']
    # Reject if gray fill present (table header)
    for r in page.rects:
        if not r.get('fill'):
            continue
        col = r.get('non_stroking_color', 0)
        if isinstance(col, (list, tuple)):
            col = sum(col) / len(col) if col else 0
        if (0.3 <= float(col) <= 0.98 and
                r['top'] <= line_bot + 2 and
                r['bottom'] >= line_top - 2):
            return False
    # Look for thin underline rect within ±4pt of line bottom
    for r in page.rects:
        if (r['bottom'] - r['top'] < 2 and
                r['x1'] - r['x0'] > 50 and
                r['x0'] <= line_x0 + 5 and
                -4 <= r['top'] - line_bot <= 6):  # ← symmetric tolerance
            return True
    return False


# ── Pre-scan: identify section boundaries ────────────────────────────
def _section_for_position(boundaries: list, page: int, top: float) -> str:
    """Return the section name active at (page, top)."""
    active = ''
    for b in boundaries:
        if b['page'] < page or (b['page'] == page and b['top'] <= top):
            active = b['segment']
        else:
            break
    return active


def _find_section_boundaries(raw_lines: list, vocab: set, 
                                 pdf_path: Path) -> list[dict]:
    """
    Returns a complete, ordered partition of the document into segments.
    Every line belongs to exactly one segment.
    
    Segment types:
      'header'               — title, meta, product bullets (before IFU)
      'ifu'                  — Instructions For Use boilerplate
      'applicable_products'  — product bullet list (between title and IFU)  
      '<section_name>'       — named sections (Overview, Coverage Policy, etc.)
      'revision_details'     — special handling
      'appendix'             — only after revision_details
    
    Returns list of:
      {'segment': str, 'page': int, 'top': float}
    sorted by (page, top), representing START of each segment.
    """
    boundaries = []
    _seen_revision = False
    _seen_ifu = False
    
    from cigna_parse_nodes import( is_footer as _is_footer )
    for l in raw_lines:
        if _is_footer(l['top'], l['text']):
            continue
        
        text = l['text'].strip()
        tl = text.lower()
        bold = l['bold']
        size = l['size']

        # Applicable products — bullet list on page 1 before IFU
        if (not _seen_ifu and l['page'] == 1 and
                not any(b['segment'] == 'applicable_products' 
                        for b in boundaries) and
                (text.startswith('•') or text.startswith('\u2022'))):
            boundaries.append({
                'segment': 'applicable_products',
                'page': l['page'],
                'top': l['top'],
            })
            continue
        
        # IFU boundary — detected by bold "INSTRUCTIONS FOR USE" line
        # or by boilerplate text starting with "The following Coverage Policy"
        if (not _seen_ifu and bold and
                ('instructions for use' in tl or
                 (size <= 11 and 'following coverage policy' in tl))):
            boundaries.append({
                'segment': 'ifu',
                'page': l['page'],
                'top': l['top'],
            })
            _seen_ifu = True
            continue
        
        # Named section boundary
        from cigna_parse_headings import classify_section
        sec = classify_section(l, vocab=vocab)
        if sec:
            if sec.lower() == 'appendix' and not _seen_revision:
                continue
            boundaries.append({
                'segment': sec,
                'page': l['page'],
                'top': l['top'],
            })
            if sec.lower() == 'revision details':
                _seen_revision = True
    
    # Prepend implicit header segment at document start
    if boundaries:
        boundaries.insert(0, {
            'segment': 'header',
            'page': 1,
            'top': 0.0,
        })

    # Append footer segment boundary
    footer_info = _find_footer_top(pdf_path)
    if footer_info:
        footer_pg, footer_top = footer_info
        boundaries.append({
            'segment': 'footer',
            'page':    footer_pg,
            'top':     footer_top,
        })
    
    # Sort by (page, top) to ensure correct order
    boundaries.sort(key=lambda b: (b['page'], b['top']))
    
    return boundaries


def _find_footer_top(pdf_path: Path) -> tuple[int, float] | None:
    """
    Returns (last_page_number, footer_top_y) by detecting the thin
    colored horizontal rule at the bottom of the last page.
    Returns None if not found.
    """
    with pdfplumber.open(str(pdf_path)) as pdf:
        last_page = pdf.pages[-1]
        pg_num = len(pdf.pages)
        for r in last_page.rects:
            if r['bottom'] - r['top'] >= 5:
                continue
            if r['x1'] - r['x0'] <= 400:
                continue
            col = r.get('non_stroking_color', None)
            if col is None or not isinstance(col, tuple):
                continue
            # Skip black and white
            if col in ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0)):
                continue
            # Any other color tuple = the footer rule
            return (pg_num, r['top'])
    return None
