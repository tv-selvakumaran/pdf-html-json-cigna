#!/usr/bin/env python3
"""
extractor.py
============
Centralised PDF line extraction for the Cigna V5 two-pass converter.

Two public functions:

    extract_raw_lines(_pdf, table_bboxes)
        ── Called by cigna_parse.py
        ── Focuses on structural boundaries: section/subsection headings,
           table regions, IFU boilerplate, page headers/footers
        ── Output: list[dict] with keys:
               page, top, x0, text, size, fontname, bold, italic, in_table

    extract_paragraph_lines(_pdf, page_num, y_start, y_end)
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

from cigna_constants import FOOTER_PATTERNS, SECTION_VOCAB



# ════════════════════════════════════════════════════════════════════════════
# Citation extraction
# ════════════════════════════════════════════════════════════════════════════

from cigna_constants import _CITATION_PATTERN, _NON_SURNAME_WORDS

def extract_citations(text: str) -> list[str]:
    _results = []
    for m in _CITATION_PATTERN.finditer(text):
        _mention = m.group(1).strip()
        _first_word = re.match(r'^[A-Za-z]+', _mention)
        if _first_word and _first_word.group(0).lower() in _NON_SURNAME_WORDS:
            continue
        _results.append(_mention)
    return _results


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


def _normalize_heading(text: str) -> str:
    """Remove letter-spacing: 'O VERVIEW' → 'overview'."""
    t = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', text)
    return t.lower().strip()


def extract_raw_lines(_pdf: PDF, table_bboxes: set,
                       column_split_x0: float = None) -> list[dict]:
    """
    Extract all lines from the full PDF for structural parsing.

    Merges superscript digits inline, skips footer lines.
    Merges split single-char section headings ('O' + 'VERVIEW' → 'Overview').

    Args:
        _pdf:     pdfplumber:PDF returned by pdfplumber.open().
        table_bboxes: set of (page_num, top, bottom) from _find_table_bboxes().
        column_split_x0: if given, words are bucketed into separate
            left/right columns by this x0 threshold BEFORE y-proximity
            bucketing, so a line straddling both columns (e.g. a
            side-by-side Table of Contents / Related Coverage
            Resources layout) is never merged into one text string.
            Default None preserves original single-column behavior.

    Returns:
        list of line dicts:
            page, top, x0, text, size, fontname, bold, italic, in_table
    """
    lines = []
    for page_num, page in enumerate(_pdf.pages, 1):
        buckets = _bucket_words_by_line(page, column_split_x0=column_split_x0)
        if not buckets:
            continue

        for yk in sorted(buckets, key=lambda k: (k[0], k[1])):
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

            if any(p.match(text) for p in FOOTER_PATTERNS):
                continue

            dominant = max(
                (w for w in ws if w['size'] >= 8.0),
                key=lambda w: len(w['text']),
                default=ws[0],
            )
            lines.append({
                'page':     page_num,
                'top':      yk[0],
                'x0':       ws[0]['x0'],
                'text':     text,
                'size':     dominant['size'],
                'fontname': dominant['fontname'],
                'bold':     _is_bold(dominant['fontname']),
                'italic':   _is_italic(dominant['fontname']),
                'in_table': any(
                    sp == page_num and en_st <= yk[0] <= en_eb
                    for sp, st, en_st, ep, eb, en_eb in table_bboxes
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

def extract_paragraph_lines(_pdf: PDF, page_num: int,
                             y_start: float = 0,
                             y_end:   float = 9999) -> list[dict]:
    """
    Extract lines from one paragraph block within a PDF page.

    Identifies bullet markers (SymbolMT •, CourierNew o, Wingdings □,
    private-use \uf0b7) and tags them with marker_type. Mixed-font buckets
    (marker + text on same y) are split: marker gets marker_type, text is
    preserved in the 'text' field.

    Args:
        _pdf:  pdfplumber:PDF returned by pdfplumber.open().
        page_num:  1-based page number.
        y_start:   Top of paragraph block (inclusive). Default 0.
        y_end:     Bottom of paragraph block (inclusive). Default 9999.

    Returns:
        list of line dicts:
            top, x0, text, size, fontname, bold, italic
            + optional  marker_type: 'bullet' | 'sub_bullet' | 'sub_sub_bullet'
    """
    lines = []
    page = _pdf.pages[page_num - 1]
    buckets = _bucket_words_by_line(page, y_start=y_start, y_end=y_end)

    # ── Pre-pass: merge split single-char headings ───────────────────
    # (e.g. 'O' at y=192 + 'VERVIEW' at y=196 → one bucket)
    sorted_yk = sorted(buckets, key=lambda k: (k[0], k[1]))
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
                    if abs(nyk[0] - yk[0]) <= 8 and nyk not in skip_yk:
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
            elif 'SymbolMT' in fn and w['size'] < 8.0 and txt == '\uf6da':
                # Registered trademark symbol (®) rendered via a
                # SymbolMT dingbat glyph -- pdfplumber can't resolve
                # this font's glyph-to-Unicode mapping, unlike the same
                # symbol rendered in a normal text font (e.g. Verdana),
                # which extracts correctly as '®' already.
                text_words.append({
                    'text': '®',
                    'x0': w['x0'],
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
            'top'          : yk[0],
            'x0'           : text_x0,
            'text'         : text,
            'size'         : dom['size'],
            'fontname'     : dom['fontname'],
            'bold'         : _is_bold(dom['fontname']),
            'italic'       : _is_italic(dom['fontname']),
            'leading_bold' : _is_bold(text_words[0]['fontname']) if text_words else False, 
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
                                 _pdf: PDF, is_cpg: bool = False, 
                                 para_cache: dict = None) -> list[dict]:
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
    _seen_references = False
    _seen_ifu = False

    # ── Pre-compute IFU bounds from colored rects ─────────────────────────
    ifu_bounds = _find_ifu_bounds(_pdf, is_cpg=is_cpg)
    _cpg_gb_bounds = _find_cpg_general_background_bounds(_pdf, ifu_bounds, para_cache) if is_cpg else None
    if _cpg_gb_bounds:
        _gb_start_page, _gb_start_y = _cpg_gb_bounds[0]
        boundaries.append({
            'segment': 'General Background',
            'page': _gb_start_page,
            'top': _gb_start_y,
        })

    # ── Pre-stamp section rect flag on raw_lines ──────────────────────────
    # Collect all colored section heading rects (thick, wide, non-white fill)
    _section_rects = []  # list of (page, top, bottom)
    _strict_section_rects = []  # strict: RGB tuple only (for in_section_rect / classify_section)
    try:
        for pg_idx, _pg in enumerate(_pdf.pages):
            for r in _pg.rects:
                col = r.get('non_stroking_color')
                _is_valid_fill = (r.get('fill') and
                                   r['x1'] - r['x0'] > 200 and
                                   r['bottom'] - r['top'] > 8 and
                                   col not in (None, 0, 1.0, (0,0,0), (1,1,1)) and
                                   not (isinstance(col, (list, tuple)) and len(col) == 3 and
                                        all(abs(c - col[0]) < 0.05 for c in col) and
                                        0.85 <= col[0] <= 0.95))
                if _is_valid_fill:
                    _section_rects.append((pg_idx + 1, r['top'], r['bottom']))
                    _is_rgb_color = isinstance(col, (list, tuple)) and len(col) == 3
                    if _is_rgb_color:
                        _strict_section_rects.append((pg_idx + 1, r['top'], r['bottom']))
    except Exception:
        pass

    from cigna_parse_nodes import( is_page_footer as _is_page_footer )
    for l in raw_lines:
        l['in_section_rect'] = any(
            pg == l['page'] and top <= l['top'] <= bottom
            for pg, top, bottom in _strict_section_rects
        )
    
        if _is_page_footer(l['top'], l['text']):
            continue
        
        text = l['text'].strip()
        tl = text.lower()
        bold = l['bold']
        size = l['size']

        # Applicable products — bullet list on page 1 before IFU/Purpose
        if (not _seen_ifu and l['page'] == 1 and
                not any(b['segment'] == 'applicable_products' 
                        for b in boundaries) and
                (text.startswith('•') or text.startswith('\u2022'))):
            _boundary_top = l['top']
            # If a colored section-heading/sub-header rect sits just
            # above this bullet (e.g. a gray-shaded category row like
            # "Tumor Necrosis Factor Inhibitors"), anchor the boundary
            # there instead, so the table's actual visual start
            # (including its heading row) falls within this segment
            # rather than being misattributed to the preceding one.
            for (rect_pg, rect_top, rect_bottom) in _section_rects:
                if (rect_pg == l['page'] and rect_bottom <= l['top'] and
                        (l['top'] - rect_bottom) < 20):
                    _boundary_top = min(_boundary_top, rect_top)
            boundaries.append({
                'segment': 'applicable_products',
                'page': l['page'],
                'top': _boundary_top - 1.0,  # small epsilon to absorb
                                              # rect-vs-bbox rounding gaps
            })
            continue

        # IFU boundary — detected by bold "INSTRUCTIONS FOR USE" or "PURPOSE" line
        # or by boilerplate text starting with "The following Coverage Policy"
        if (not _seen_ifu and bold and
                ('instructions for use' in tl or
                 tl.strip() == 'purpose' or
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
        sec = classify_section(l, vocab=vocab, is_cpg=is_cpg)

        # Detect Appendix heading at 8.5pt after References or Revision Details
        if not sec and l['bold'] and l['size'] >= 8.5 and l['x0'] < 70:
            _anorm = _normalize_heading(l['text'])
            if _anorm.startswith('appendix') and (_seen_references or _seen_revision):
                sec = 'Appendix'

        if sec:
            if sec.lower() == 'appendix' and not _seen_revision and not _seen_references:
                continue
            boundaries.append({
                'segment': sec,
                'page': l['page'],
                'top': l['top'],
            })
            if sec.lower() in ('references',):
                _seen_references = True
            if sec.lower() == 'revision details':
                _seen_revision = True
    
    # ── Detect Overview after IFU end (drug policies without colored rect) ──
    if ifu_bounds and not any(b['segment'].lower() == 'overview'
                               for b in boundaries):
        ifu_end_page, ifu_end_y = ifu_bounds[1]
        _found_overview_line = False
        for l in raw_lines:
            # Line must appear after IFU end rect
            if (l['page'] > ifu_end_page or
                    (l['page'] == ifu_end_page and l['top'] > ifu_end_y)):
                if (l['bold'] and
                        _normalize_heading(l['text']) == 'overview'):
                    # Only treat this as an implicit top-level section if
                    # it genuinely precedes every other already-detected
                    # section boundary -- if a real section (e.g.
                    # "Background") was already found starting before
                    # this line, "Overview" is a subsection heading
                    # WITHIN that section, not a sibling section of its
                    # own.
                    _later_boundaries = [
                        (b['page'], b['top']) for b in boundaries
                        if (b['page'], b['top']) > (ifu_end_page, ifu_end_y)
                    ]
                    _next_boundary_after_ifu = min(_later_boundaries) if _later_boundaries else None
                    _precedes_all_existing = (
                        _next_boundary_after_ifu is None or
                        (l['page'], l['top']) < _next_boundary_after_ifu
                    )
                    if _precedes_all_existing:
                        boundaries.append({
                            'segment': 'Overview',
                            'page': l['page'],
                            'top': l['top'],
                        })
                    _found_overview_line = True
                    break
        if not _found_overview_line:
            # Only insert an inferred Overview boundary if there's
            # actual content between IFU's end and wherever raw_lines
            # continues -- an empty gap needs no dummy section.
            _has_content_after_ifu = any(
                (l['page'] > ifu_end_page or
                 (l['page'] == ifu_end_page and l['top'] > ifu_end_y)) and
                l['text'].strip()
                for l in raw_lines
            )
            if _has_content_after_ifu:
                boundaries.append({
                    'segment': 'Overview',
                    'page': ifu_end_page,
                    'top': ifu_end_y,
                })

    # Prepend implicit header segment at document start
    if boundaries:
        boundaries.insert(0, {
            'segment': 'header',
            'page': 1,
            'top': 0.0,
        })

    # Append footer segment boundary
    footer_info = _find_footer_top(_pdf, is_cpg=is_cpg)
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


def _find_footer_top(_pdf: PDF, is_cpg: bool = False) -> tuple[int, float] | None:
    """
    Returns (last_page_number, footer_top_y) by detecting the thin
    colored (or, for cpg-family documents, black) horizontal rule at
    the bottom of the last page. Returns None if not found.
    """
    last_page = _pdf.pages[-1]
    pg_num = len(_pdf.pages)
    for r in last_page.rects:
        if r['bottom'] - r['top'] >= 5:
            continue
        if r['x1'] - r['x0'] <= 400:
            continue
        col = r.get('non_stroking_color', None)
        if col is None:
            continue
        _is_black = (col == (0.0, 0.0, 0.0) or col == 0 or col == 0.0)
        _is_white = (col == (1.0, 1.0, 1.0) or col == 1 or col == 1.0)
        if _is_white:
            continue
        if _is_black and not is_cpg:
            continue
        if not (isinstance(col, tuple) or _is_black):
            continue
        return (pg_num, r['top'])
    return None


def _find_ifu_bounds(_pdf: PDF, is_cpg: bool = False) -> tuple[tuple[int,float], tuple[int,float]] | None:
    """
    Returns ((start_page, start_top_y), (end_page, end_top_y)) for IFU section.
    Finds first two thin colored horizontal rects that appear before
    the last page (which has the footer rect).
    For cpg-family documents, which use black (not colored) horizontal
    rules to mark section boundaries, black fill is also accepted --
    the width threshold (>400) already filters out narrow incidental
    black rule/underline artifacts, which are the reason black is
    excluded for other document families.
    Returns None if not found.
    """
    n_pages = len(_pdf.pages)
    colored_rects = []
    for pg_idx, page in enumerate(_pdf.pages):
        if pg_idx == n_pages - 1:
            break
        for r in page.rects:
            col = r.get('non_stroking_color')
            _excluded_colors = (None, 1.0, (1, 1, 1))
            if not is_cpg:
                _excluded_colors = _excluded_colors + (0, (0, 0, 0))
            if (r.get('fill') and
                    r['x1'] - r['x0'] > 400 and
                    r['bottom'] - r['top'] < 5 and
                    col not in _excluded_colors):
                colored_rects.append((pg_idx + 1, r['top']))
        if len(colored_rects) >= 2:
            break

    if len(colored_rects) >= 2:
        return (colored_rects[0], colored_rects[1])
    elif len(colored_rects) == 1:
        return (colored_rects[0], colored_rects[0])
    return None


def _bucket_words_by_line(page, y_start: float = 0, y_end: float = 9999,
                           column_split_x0: float = None) -> dict:
    words = page.extract_words(
        extra_attrs=['fontname', 'size'],
        keep_blank_chars=False, x_tolerance=3, y_tolerance=3)

    buckets: dict = {}
    for w in words:
        top = w['top']
        if not (y_start <= top <= y_end):
            continue
        _col = (0 if column_split_x0 is None or w['x0'] < column_split_x0 else 1)
        matched_key = None
        for k in buckets:
            if k[1] == _col and abs(k[0] - top) <= 2.0:
                matched_key = k
                break
        yk = matched_key if matched_key is not None else (top, _col)
        buckets.setdefault(yk, []).append(w)

    return buckets


def construct_phrases(page_ranges: list[tuple]) -> list[dict]:
    """
    Given a list of (page_obj, start_top, end_top) ranges -- typically
    one entry for a single-page paragraph, or multiple entries for a
    paragraph spanning several pages -- group all words across these
    ranges into phrases: maximal runs of consecutive words (within one
    line, across line-wraps, and across the page boundaries between
    consecutive ranges) sharing identical formatting (bold, italic,
    size, fontname).

    For all but the last range, end_top should be set high enough to
    capture the rest of that page's content belonging to this
    paragraph (typically 9999, since a mid-paragraph page boundary
    means "the rest of this page belongs to this paragraph").
    For all but the first range, start_top should typically be 0 (the
    paragraph's continuation starts at the top of the new page).

    Returns a list of phrase dicts: {text, bold, italic, size,
    fontname, top, x0, page} in reading order.
    """
    def _fmt_key(w):
        return (_is_bold(w['fontname']), _is_italic(w['fontname']),
                w['size'], w['fontname'])

    phrases = []
    current = None

    for page, start_top, end_top in page_ranges:
        buckets = _bucket_words_by_line(page, y_start=start_top, y_end=end_top)
        sorted_keys = sorted(buckets, key=lambda k: (k[0], k[1]))
        _page_num = page.page_number

        for yk in sorted_keys:
            ws = sorted(buckets[yk], key=lambda w: w['x0'])
            for w in ws:
                if w['size'] < 6.0:
                    continue
                key = _fmt_key(w)
                if current is not None and current['_fmt'] == key:
                    current['text_parts'].append(w['text'])
                else:
                    if current is not None:
                        phrases.append({
                            'text': ' '.join(current['text_parts']),
                            'bold': current['_fmt'][0],
                            'italic': current['_fmt'][1],
                            'size': current['_fmt'][2],
                            'fontname': current['_fmt'][3],
                            'top': current['top'],
                            'x0': current['x0'],
                            'page': current['page'],
                        })
                    current = {
                        'text_parts': [w['text']],
                        '_fmt': key,
                        'top': yk[0],
                        'x0': w['x0'],
                        'page': _page_num,
                    }
    if current is not None:
        phrases.append({
            'text': ' '.join(current['text_parts']),
            'bold': current['_fmt'][0],
            'italic': current['_fmt'][1],
            'size': current['_fmt'][2],
            'fontname': current['_fmt'][3],
            'top': current['top'],
            'x0': current['x0'],
            'page': current['page'],
        })

    return phrases


def _extract_authors_and_year(text: str, is_reference: bool = False) -> tuple[frozenset, str] | None:
    """
    Extract (author_surnames, year) from either an in-text citation
    mention (e.g. 'K Maru and Gupta, 2016', 'Homsi and Gaffey, 2022',
    'Payne, et al., 2025') or a full reference-list citation (e.g.
    '9. K Maru Y, Gupta Y. Nasal Endoscopy... 2016 Jun;68(2):202-6.').

    author_surnames is the set of surnames actually named in the
    text -- for an in-text mention this may be just the first author
    (when 'et al.' is used) or two authors (when joined by 'and');
    for a reference list entry, ALL listed authors are extracted, so
    the mention's author set can be checked as a SUBSET of the
    reference's full set, rather than requiring exact equality.

    Returns None if no plausible year or author name is found.
    """
    from cigna_constants import CITATION_ORG_NAMES
    _stripped = text.lower().lstrip('0123456789.) ')
    for full_name, abbr in CITATION_ORG_NAMES:
        if _stripped.startswith(full_name.lower()) or _stripped.startswith(abbr.lower()):
            _year_m = re.search(r'\b(19\d{2}|20\d{2})\b', text)
            if _year_m:
                return (frozenset({full_name.lower()}), _year_m.group(1))
            return None

    _text = re.sub(r'^\d+[.)]\s*', '', text.strip()) if is_reference else text.strip()
    _m = re.match(r'^([A-Z][A-Za-z\-]+)', _text)
    _surname = _m.group(1) if _m else None
    if _surname and _surname.lower() in _NON_SURNAME_WORDS:
        return None

    if is_reference:
        # Reference list: authors are a comma-separated run of
        # "Surname Initials" pairs, up to the first sentence-ending
        # period (the title starts after that).
        _authors_part = _text.split('.')[0]
        _surnames = set()
        for _tok in _authors_part.split(','):
            _m = re.match(r'\s*((?:[A-Z]\s+)?[A-Z][A-Za-zÀ-ÿ\-]+)', _tok)
            if _m:
                _surnames.add(_m.group(1).strip().lower())
        _year_m = re.search(r'\b(19\d{2}|20\d{2})\b', _text)
        if not _surnames or not _year_m:
            return None
        return (frozenset(_surnames), _year_m.group(1))
    else:
        # In-text mention: one or two authors joined by "and"/"&",
        # optionally followed by "et al.", then a year.
        _m = re.match(
            r'^((?:[A-Z]\s+)?[A-Z][A-Za-zÀ-ÿ\-]+)'
            r'(?:\s+(?:and|&)\s+((?:[A-Z]\s+)?[A-Z][A-Za-zÀ-ÿ\-]+))?'
            r'(?:,?\s+et\s+al\.?)?,?\s+(\d{4})$',
            _text.strip())
        if not _m:
            return None
        _first, _second, _year = _m.groups()
        _surnames = {_first.strip().lower()}
        if _second:
            _surnames.add(_second.strip().lower())
        return (frozenset(_surnames), _year)


def _citation_matches_reference(mention_sig: tuple, ref_sig: tuple) -> bool:
    """True if the mention's author set is a subset of the reference's
    author set, and years match."""
    _mention_authors, _mention_year = mention_sig
    _ref_authors, _ref_year = ref_sig
    return _mention_year == _ref_year and _mention_authors.issubset(_ref_authors)



def _find_cpg_general_background_bounds(
        _pdf: PDF, ifu_bounds: tuple | None, para_cache: dict) -> tuple[tuple[int, float], tuple[int, float]] | None:
    """
    For cpg-family documents: the General Background/Documentation
    Guidelines/Literature Review content is bounded by two thick,
    black horizontal lines, appearing somewhere after IFU ends --
    but NOT necessarily immediately after it, since other sections
    (e.g. Coding Information) may come first. Scans every consecutive
    pair of black lines after IFU, in order, and returns the first
    pair whose enclosed content contains a genuine General Background
    subsection heading (Description, General Background, Documentation
    Guidelines, or Literature Review). Returns None if no such pair
    is found.
    """
    if not ifu_bounds:
        return None
    _ifu_end_page, _ifu_end_y = ifu_bounds[1]

    black_lines = []
    for pg_idx, page in enumerate(_pdf.pages):
        pg = pg_idx + 1
        if pg < _ifu_end_page:
            continue
        for r in page.rects:
            if pg == _ifu_end_page and r['top'] <= _ifu_end_y:
                continue
            col = r.get('non_stroking_color')
            if (r.get('fill') and
                    r['x1'] - r['x0'] > 400 and
                    r['bottom'] - r['top'] < 5 and
                    col in (0, (0, 0, 0))):
                black_lines.append((pg, r['top']))

    _gb_headings_norm = {
        'description', 'generalbackground', 'documentationguidelines', 'literaturereview',
    }

    from cigna_parse_headings import normalize_heading

    for i in range(len(black_lines) - 1):
        _start = black_lines[i]
        _end = black_lines[i + 1]
        _start_pg, _start_y = _start
        _end_pg, _end_y = _end

        _found_heading = False
        for pg in range(_start_pg, _end_pg + 1):
            for l in para_cache.get(pg, []):
                if pg == _start_pg and l['top'] <= _start_y:
                    continue
                if pg == _end_pg and l['top'] >= _end_y:
                    continue
                if not l.get('bold'):
                    continue
                _norm = normalize_heading(l['text'].strip())
                if _norm in _gb_headings_norm:
                    _found_heading = True
                    break
            if _found_heading:
                break

        if _found_heading:
            return (_start, _end)

    return None
