#!/usr/bin/env python3
"""
cigna_parse_tables.py
=====================
Table detection, reconstruction, and tree injection for the Cigna V5 converter.
"""

from __future__ import annotations
import re
from collections import defaultdict
from pathlib import Path

import pdfplumber

from cigna_parse_nodes import (
    TableNode, ParagraphBlockNode, SubsectionNode, SectionNode,
    ParagraphBlock,
)

from cigna_parse_headings import (
    normalize_heading, classify_section,
)


try:
    from reconstruct_cigna_table import (
        reconstruct_availability_table,
        reconstruct_drug_quantity_table,
        reconstruct_revision_table,
        reconstruct_moa_table,
        reconstruct_fda_dosing_table,
    )
    HAS_RECONSTRUCTORS = True
except ImportError:
    HAS_RECONSTRUCTORS = False



def _section_at(boundaries: list, page: int, top: float) -> str:
    """Return the section name active at (page, top), given a sorted
    list of {'section': str, 'page': int, 'top': float} boundaries."""
    if not boundaries:
        return ''
    active = ''
    for b in boundaries:
        if b['page'] < page or (b['page'] == page and b['top'] <= top):
            active = b['segment']
        else:
            break
    return active


# ════════════════════════════════════════════════════════════════════════════
# Spurious table detection
# ════════════════════════════════════════════════════════════════════════════

def _is_spurious_table(tdata: list, bbox: tuple, page_bboxes: list,
                       page=None) -> bool:
    """
    Return True if this pdfplumber table is an artifact, not a real table.

    Spurious tables are:
    - Very narrow (height < 30pt) = horizontal rule line
    - Single-column with all-empty cells = green rule or empty border
    - Single-column strictly contained within another table = sub-cell
    - Contains a green-filled rect (Cigna section heading stripe)
    """
    if not tdata:
        return True
    row0 = tdata[0] if tdata else []
    height = bbox[3] - bbox[1]
    cols = len(row0)

    # Contains a Cigna green section heading stripe = not a real table
    # Green color: R~0, G~0.6-0.75, B~0.2-0.4
    if page is not None:
        for r in page.rects:
            if not r.get('fill'): continue
            fc = r.get('non_stroking_color', 0)
            if (isinstance(fc, (list, tuple)) and len(fc) == 3 and
                    fc[0] < 0.1 and 0.5 < fc[1] < 0.85 and 0.1 < fc[2] < 0.5 and
                    r['top'] >= bbox[1] - 2 and r['bottom'] <= bbox[3] + 2):
                return True

    # Very narrow = horizontal rule
    if height < 30 and cols <= 2:
        return True

    # Single-column checks
    if cols == 1:
        # All cells empty
        all_empty = all(
            not (cell or '').strip()
            for row in tdata for cell in (row or [])
        )
        if all_empty:
            return True
        # Strictly contained within another bbox on same page
        for ob in page_bboxes:
            if ob is bbox:
                continue
            if (ob[0] < bbox[0] and ob[1] <= bbox[1] and
                    ob[2] > bbox[2] and ob[3] >= bbox[3]):
                return True
    # Multi-column table strictly contained within another bbox = sub-table artifact
    for ob in page_bboxes:
        if ob is bbox:
            continue
        if (ob[0] <= bbox[0] and ob[1] <= bbox[1] and
                ob[2] >= bbox[2] and ob[3] >= bbox[3] and
                (ob[2]-ob[0]) > (bbox[2]-bbox[0]) * 1.5):
            return True
    # Single-column (or 2-col where col1 always empty) body text = bordered paragraph
    _col1_always_empty = (cols == 2 and all(
        not ((row[1] or '') if len(row) > 1 else '').strip() if row else True
        for row in tdata))
    if (cols == 1 or _col1_always_empty) and tdata:
        first_cell = ((tdata[0][0] if tdata[0] else '') or '').strip()
        header_starts = ('Type of', 'Summary', 'Date', 'Product', 'Strength',
                         'HCPCS', 'CPT', 'ICD', 'Retail', 'Dosage', 'Agent',
                         'Medication', 'Condition', 'Brand', 'Drug')
        # Only classify as body text if most rows are long sentences
        # (not short drug names/entries)
        non_empty = [(row[0] or '').strip() for row in tdata if (row[0] if row else '') and (row[0] or '').strip()]
        long_rows = sum(1 for c in non_empty if len(c) > 60)
        _is_indented = bbox[0] > 80
        if (len(first_cell) > 15 and ' ' in first_cell and
                not any(first_cell.startswith(h) for h in header_starts) and
                (first_cell[0].isupper() or _is_indented) and
                long_rows > len(non_empty) * 0.4):  # majority are long sentences
            return True
    # Single-column with only 1 non-empty row out of many = bordered paragraph
    if cols == 1 and len(tdata) > 3:
        non_empty = sum(1 for row in tdata
                        if any((c or '').strip() for c in (row or [])))
        if non_empty <= 1:
            return True

    # 1-row, 2-col table where col0 is empty and col1 is a long footnote sentence
    # e.g. ph_1705 p5, ph_2016 p11 — bordered footnote/annotation boxes
    if len(tdata) == 1 and cols == 2:
        c0 = (tdata[0][0] or '').strip()
        c1 = (tdata[0][1] or '').strip()
        if not c0 and len(c1) > 40 and ' ' in c1:
            return True

    return False



def _true_table_top(bbox, page_rects) -> float:
    """
    pdfplumber bbox[1] is wrong for open-top-border continuation tables:
    it reports the bottom of the missing top border row, not the visual top.
    Detect this by looking for vertical border rects at the table's left/right
    x-bounds that extend above bbox[1] — their top is the real table top.
    """
    x_left  = bbox[0]
    x_right = bbox[2]
    real_top = bbox[1]
    for r in page_rects:
        # Vertical rect: width < 2pt, at left or right table border
        if (r['x1'] - r['x0'] < 2 and
                r['bottom'] >= bbox[1] - 2 and
                r['top'] < real_top and
                (abs(r['x0'] - x_left)  < 3 or
                 abs(r['x0'] - x_right) < 3)):
            real_top = r['top']
    return real_top


# ════════════════════════════════════════════════════════════════════════════
# Table info (replaces find_table_bboxes)
# ════════════════════════════════════════════════════════════════════════════

def find_table_info(pdf_path: Path, para_cache: dict = None) -> tuple:
    """
    Scan all pages for tables. Returns (tables, bboxes) where:
      tables: list of dicts with page, bbox, table_type, title, row0, rows,
              cols, is_continuation, covered
      bboxes: set of (page, top, bot) tuples used by extract_raw_lines

    para_cache: optional dict {page: [line_dicts]} from extract_paragraph_lines.
                If provided, bold lines from para_cache are used for title lookup
                instead of pdfplumber word extraction (more accurate).
    """
    results = []
    covered_bboxes = set()
    prev_bbox = None  # last REAL table bbox on previous page

    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = pdf.pages
        for pg, page in enumerate(pages, 1):
            tables = page.find_tables()
            if not tables:
                prev_bbox = None
                continue

            words = page.extract_words(extra_attrs=['fontname', 'size'])
            tiny_words = page.extract_words(
                extra_attrs=['size'], x_tolerance=2, y_tolerance=2)
            tiny_by_x: dict = defaultdict(list)
            for w in tiny_words:
                if w['size'] < 8.0 and len(w['text']) <= 3:
                    xk = round(w['x0'] / 8) * 8
                    tiny_by_x[xk].append(w)
            tiny_cols = sum(1 for ws in tiny_by_x.values() if len(ws) >= 4)

            # Collect all bboxes on this page for containment check
            page_bboxes = [t.bbox for t in tables]

            for t in tables:
                tdata = t.extract() or []
                row0 = tdata[0] if tdata else []
                bbox = t.bbox

                # Skip spurious tables — don't update prev_bbox
                if _is_spurious_table(tdata, bbox, page_bboxes, page=page):
                    results.append({
                        'page': pg,
                        'bbox': tuple(round(x, 1) for x in bbox),
                        'table_type': 'spurious',
                        'title': '',
                        'row0': str([str(c)[:20] if c else None for c in row0[:5]]),
                        'rows': len(tdata),
                        'cols': len(row0),
                        'is_continuation': False,
                        'covered': False,
                    })
                    continue

                # Classify
                has_product = False
                if any((c or '').strip() in ('Product', 'Product Name') for c in row0):
                    # Only DQL if col1 is a quantity/strength heading, not 'Criteria for Use'
                    _col1 = (row0[1] or '').strip().lower() if len(row0) > 1 else ''
                    if 'criteria' not in _col1 and 'use' not in _col1:
                        has_product = True
                has_criteria = any(
                    re.sub(r'\s+', '', (c or '').strip()).rstrip(':') == 'Criteria'
                    for c in row0)
                is_rev  = bool(row0 and (row0[0] or '').strip() == 'Type of Revision')
                is_avail = tiny_cols >= 4 and not has_product
                is_hcpcs = bool(row0 and any(
                    'HCPCS' in (c or '') or 'CPT' in (c or '')
                    for c in row0[:2]))
                # Also detect HCPCS when pdfplumber misses left column:
                # look for 'HCPCS' or 'CPT' words to left of table bbox
                if not is_hcpcs:
                    _left_words = ' '.join(
                        w['text'] for w in words
                        if w['x1'] <= bbox[0] + 10 and
                        bbox[1] - 5 <= w['top'] <= bbox[3] + 5)
                    if 'HCPCS' in _left_words or 'CPT' in _left_words:
                        is_hcpcs = True
                flat_r0r2 = ' '.join(str(c) for row in (tdata[:3] or [])
                                     for c in (row or []))
                # MOA: 'Mechanism of Action' may be split across cells/rows
                _flat_r0r2_nospace = ' '.join(
                    (c or '').strip() for row in (tdata[:3] or [])
                    for c in (row or []) if (c or '').strip())
                is_moa = ('Mechanism of Action' in _flat_r0r2_nospace and
                          (row0[0] or '').strip() == '')
                is_icd = bool(row0 and 'ICD-10' in (row0[0] or ''))
                is_eua = bool(len(row0) >= 2 and
                              (row0[0] or '').strip() == 'Date' and
                              'EUA' in (row0[1] or ''))
                flat_r0 = ' '.join(str(c) for c in (row0 or []))
                is_fda_rx = ('Drug' in flat_r0 and 'Prescribing' in flat_r0)
                is_rev_flex = (not is_rev and row0 and
                               any('Summary of Changes' in (c or '')
                                   for c in row0))

                # Continuation: same left x-bound as last REAL table, near top of page
                # Right x-bound may differ if column count changes across pages
                _visual_top = _true_table_top(bbox, page.rects)
                is_cont = (prev_bbox is not None and
                           abs(bbox[0] - prev_bbox[0]) < 5 and
                           abs(bbox[2] - prev_bbox[2]) < 30 and
                           _visual_top < 120)

                # Title: bold line immediately above table
                # Use para_cache if available (more accurate than pdfplumber words)
                table_top = bbox[1]
                _t_title = ''
                if para_cache is not None:
                    page_lines = para_cache.get(pg, [])
                    # Walk backwards to collect consecutive bold lines
                    # immediately above the table (handles 2-line titles).
                    # Stop at a non-bold line or gap > 14pt.
                    _above = sorted(
                        [l for l in page_lines if l['top'] < table_top],
                        key=lambda l: -l['top'])  # nearest first
                    _collected = []
                    _prev_top = table_top
                    for _l in _above:
                        if _prev_top - _l['top'] > 20:  # gap > one line
                            break
                        if _l.get('bold', False):
                            # Stop if last collected line is short (heading)
                            # AND this line is at same x0 AND larger font
                            # e.g. 'Dosing' (size=11) above 'Table 1.' (size=9)
                            if _collected:
                                _last = _collected[-1]
                                # New candidate is a heading if:
                                # - it is short text (< 30 chars)
                                # - starts at same x0 as last collected
                                # - has larger font than last collected
                                _new_short = len(_l.get('text','')) < 30
                                _same_x = abs(_l.get('x0',0) - _last.get('x0',0)) < 5
                                _larger = _l.get('size',0) > _last.get('size',0) + 0.5
                                if _new_short and _same_x and _larger:
                                    break  # stop, don't add heading
                            _collected.append(_l)
                            _prev_top = _l['top']
                        else:
                            break
                    # Build title: sort by top, then x0 within lines
                    # that are within 6pt of each other (superscripts)
                    # so left-aligned title text always comes before
                    # right-aligned footnote superscripts on the same line
                    def _title_key(l):
                        top = l['top']
                        # Find nearest collected line within 6pt
                        for _other in _collected:
                            if _other is not l and abs(_other['top'] - top) <= 6:
                                # Group with that line: use its top as sort key
                                top = max(top, _other['top'])
                        return (top, l.get('x0', 0))
                    title_lines = sorted(_collected, key=_title_key)
                    title = ' '.join(l['text'] for l in title_lines)[:80]
                    # If no title found and table is near top of page,
                    # check bottom of previous page for bold title lines
                    if not title and table_top < 120 and pg > 1:
                        prev_lines = para_cache.get(pg - 1, [])
                        _prev_bold = [l for l in prev_lines
                                      if l.get('bold') and l['top'] > 650]
                        if _prev_bold:
                            _prev_bold.sort(key=lambda l: l['top'])
                            title = ' '.join(l['text'] for l in _prev_bold)[:80]
                else:
                    title_words = [w for w in words
                                   if table_top - 16 <= w['top'] < table_top
                                   and 'Bold' in w.get('fontname', '')]
                    title = ' '.join(w['text'] for w in title_words)[:80]
                is_titled = bool(re.match(r'(Appendix\s+)?Table\s+\d+[.]', title))
                # Title-based detection for known table title patterns
                # Normalize spaced-out text e.g. 'A PPENDIX' -> 'APPENDIX'
                _title_norm = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', title)
                _title_lc = _title_norm.lower()
                is_titled_known = (
                    'preferred and non-preferred products' in _title_lc or
                    'preferred products' in _title_lc or
                    'non-preferred products' in _title_lc or
                    'by indication' in _title_lc or
                    'drug availability' in _title_lc or
                    'dosage forms' in _title_lc or
                    'fda approved' in _title_lc or
                    'fda recommended' in _title_lc or
                    'appendix' in _title_lc or
                    'prescription drug lists' in _title_lc or
                    'simon broome' in _title_lc or
                    'dutch lipid' in _title_lc or
                    'laboratory diagnosis' in _title_lc or
                    'diagnostic criteria' in _title_lc or
                    'reauthorization criteria' in _title_lc or
                    'dose conversion' in _title_lc or
                    'dosing regimen' in _title_lc or
                    'indications' in _title_lc or
                    'individual and family plans' in _title_lc or
                    'employer plans' in _title_lc or
                    'fda approved indication' in _title_lc or
                    'fda recommended dosing' in _title_lc or
                    'fda approved products' in _title_lc or
                    _title_lc.strip() in ('dosing', 'drug availability',
                                          'dosage forms for this indication',
                                          'follistim pen dose conversion table*') or
                    re.match(r'Table\s+\d+[:\s]', _title_norm) is not None
                )

                flat_r0_np = flat_r0
                is_nonpref = ('Exception Criteria' in flat_r0_np or
                              ('Non-Preferred' in flat_r0_np and
                               'Criteria' in flat_r0_np) or
                              'Criteria for Use' in flat_r0_np)

                is_drug_equiv = ('Non-Covered Brand' in flat_r0 or
                                 'Non-Covered Product' in flat_r0 or
                                 'Bioequivalent' in flat_r0)

                # Determine type
                if is_rev:
                    ttype = 'revision'
                elif has_product:
                    ttype = 'dql'
                elif is_avail:
                    ttype = 'availability'
                elif has_criteria:
                    ttype = 'criteria'
                elif is_hcpcs:
                    ttype = 'hcpcs'
                elif is_moa:
                    ttype = 'moa'
                elif is_icd:
                    ttype = 'hcpcs'
                elif is_eua:
                    ttype = 'generic'
                elif is_fda_rx:
                    ttype = 'generic'
                elif is_rev_flex:
                    ttype = 'revision'
                elif is_titled or is_titled_known:
                    ttype = 'generic'
                elif is_nonpref:
                    ttype = 'generic'
                elif is_drug_equiv:
                    ttype = 'generic'
                elif ('Medication' in flat_r0 and
                      'Mode of Administration' in flat_r0r2):
                    ttype = 'generic'
                elif ('Condition' in flat_r0 and
                      'Criteria for Use' in flat_r0r2):
                    ttype = 'generic'
                elif any(kw in flat_r0 for kw in (
                         'Compound Name', 'Drug Name', 'Comments',
                         'Prescribing Information', 'Ingredient')):
                    ttype = 'generic'
                elif is_cont:
                    ttype = 'continuation'
                elif (len(row0) >= 2 and (
                        sum(1 for c in (tdata[0] or []) if (c or '').strip()) >= 2 or
                        any(sum(1 for c in (row or []) if (c or '').strip()) >= 2
                            for row in tdata[1:]) or
                        len(tdata) >= 3)) or\
                        (len(row0) == 1 and len(tdata) >= 5):
                    # Catch-all: multi-col with >=2 non-empty cells (includes
                    # genuine 1-row tables like ph_8007 p2 Obizur), or
                    # single-col list with >=5 rows
                    ttype = 'generic'
                else:
                    ttype = 'unknown'

                covered = ttype != 'unknown'
                if covered:
                    expand = 30 if ttype == 'criteria' else 5
                    covered_bboxes.add((pg, max(0, bbox[1] - expand), bbox[3]))

                results.append({
                    'page': pg,
                    'bbox': tuple(round(x, 1) for x in bbox),
                    'table_type': ttype,
                    'title': title,
                    'row0': str([str(c)[:20] if c else None for c in row0[:5]]),
                    'rows': len(tdata),
                    'cols': len(row0),
                    'is_continuation': is_cont,
                    'covered': covered,
                })
                # Only update prev_bbox for real (non-spurious) tables
                prev_bbox = bbox

    return results, covered_bboxes


# ════════════════════════════════════════════════════════════════════════════
# Generic table renderer
# ════════════════════════════════════════════════════════════════════════════

def render_generic_table(tdata: list, has_header: bool = True) -> str:
    """Render a generic table. If has_header=False, treat all rows as data."""
    from html import escape as esc
    if not tdata:
        return ''

    def _fmt_cell(txt: str) -> str:
        if not txt:
            return ''
        parts = txt.strip().split('\n')
        return '<br>'.join(esc(p.strip()) for p in parts if p.strip())

    header_row = tdata[0] if tdata else []
    num_cols   = max(len(row) for row in tdata) if tdata else 0
    # Use cols that have content in the header OR in any data row —
    # tables like ip_0166 have data in col 0 but header label in col 1.
    _header_cols = {ci for ci in range(len(header_row))
                    if (header_row[ci] or '').strip()}
    _data_cols   = {ci for ci in range(num_cols)
                    if any(((row[ci] if ci < len(row) else '') or '').strip()
                           for row in tdata[1:])}

    # Remap header labels to data columns when offset by 1
    # e.g. header 'Non-Covered Brand' at col1, data at col0 → remap to col0
    # Pad to num_cols so data cols beyond header row length don't cause IndexError
    _remapped_header = list(header_row) + [None] * (num_cols - len(header_row)) \
                       if header_row else [None] * num_cols
    for ci in sorted(_header_cols - _data_cols):
        target = ci - 1
        if (target in _data_cols and
                not (_remapped_header[target] or '').strip()):
            _remapped_header[target] = header_row[ci]
            _remapped_header[ci] = None

    # Only keep cols with a non-empty header after remapping
    used_cols = sorted(
        ci for ci in sorted(_header_cols | _data_cols)
        if (_remapped_header[ci] or '').strip()
    )
    if not used_cols:
        used_cols = sorted(_header_cols | _data_cols) or list(range(num_cols))

    # If no header, treat all rows as data
    if not has_header:
        lines = ['  <table border="1" bordercolor="#000000" cellpadding="4" '
                 'cellspacing="0" style="width:100%;font-size:9pt">',
                 '   <tbody>']
        for row in tdata:
            _nonempty_nh = [(ci, (row[ci] or '').strip())
                            for ci in range(len(row))
                            if (row[ci] or '').strip()]
            # Subgroup heading: one non-empty cell, not in col 0
            if (len(_nonempty_nh) == 1 and len(row) > 1 and
                    _nonempty_nh[0][0] != 0):
                lines.append(
                    f'    <tr style="background:#e0e0e0">'
                    f'<td colspan="{len(used_cols)}" style="font-weight:bold">'
                    f'{_fmt_cell(_nonempty_nh[0][1])}</td></tr>'
                )
                continue
            values = [(row[ci] or '').strip()
                      for ci in range(len(row)) if (row[ci] or '').strip()]
            if not values: continue
            lines.append('    <tr>')
            while len(values) < len(used_cols): values.append('')
            for vi in range(len(used_cols)):
                val = values[vi] if vi < len(values) else ''
                lines.append(f'     <td style="vertical-align:top">{_fmt_cell(val)}</td>')
            lines.append('    </tr>')
        lines += ['   </tbody>', '  </table>']
        return '\n'.join(lines)
    lines = ['  <table border="1" bordercolor="#000000" cellpadding="4" '
             'cellspacing="0" style="width:100%;font-size:9pt">',
             '   <thead>',
             '    <tr style="background:#e8e8e8">']
    for ci in used_cols:
        cell = _remapped_header[ci] if ci < len(_remapped_header) else ''
        lines.append(f'     <th>{_fmt_cell(cell or "")}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']
    for row in tdata[1:]:
        # Subgroup heading: exactly one non-empty cell across all columns.
        # Render as a gray full-width colspan row (same pattern as MOA table).
        _nonempty = [(ci, (row[ci] or '').strip())
                     for ci in range(len(row))
                     if (row[ci] or '').strip()]
        # Subgroup heading: exactly one non-empty cell, and it is NOT in
        # col 0 (col 0 = primary data column; content there = data row).
        if (len(_nonempty) == 1 and len(row) > 1 and
                _nonempty[0][0] != 0):
            lines.append(
                f'    <tr style="background:#e0e0e0">'
                f'<td colspan="{len(used_cols)}" style="font-weight:bold">'
                f'{_fmt_cell(_nonempty[0][1])}</td></tr>'
            )
            continue
        # Get non-empty cells in column order
        values = [(row[ci] or '').strip()
                  for ci in range(len(row))
                  if (row[ci] or '').strip()]
        if not values:
            continue
        lines.append('    <tr>')
        # Pad to match header column count
        while len(values) < len(used_cols):
            values.append('')
        for vi in range(len(used_cols)):
            val = values[vi] if vi < len(values) else ''
            lines.append(
                f'     <td style="vertical-align:top">{_fmt_cell(val)}</td>')
        lines.append('    </tr>')
    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Table node builder
# ════════════════════════════════════════════════════════════════════════════

def _lookup_title(pi, bbox, table_info):
    """Find the title from find_table_info results matching this page/bbox."""
    if not table_info:
        return ''
    for entry in table_info:
        if (entry['page'] == pi + 1 and
                abs(entry['bbox'][1] - bbox[1]) < 5):
            return entry['title']
    return ''


# ════════════════════════════════════════════════════════════════════════════
# Table injection into document tree
# ════════════════════════════════════════════════════════════════════════════

def _inject_into_active_subsection(section, tn, table_info=None):
    """Find the subsection active at (tn.page, tn.top) and insert the table
    immediately after its title paragraph. Returns True if injected."""
    active_sub = None
    for child in section.children:
        if not isinstance(child, SubsectionNode):
            continue
        if child.page > tn.page:
            break
        if child.page == tn.page and child.top > tn.top:
            break
        active_sub = child
    if active_sub is None:
        return False

    # Find the title paragraph — a ParagraphBlockNode whose text
    # starts with the table title — and insert immediately after it.
    title = (tn.title or '').strip() if hasattr(tn, 'title') else ''
    insert_idx = len(active_sub.children)  # fallback: end
    for i, sc in enumerate(active_sub.children):
        if isinstance(sc, ParagraphBlockNode):
            txt = (sc.block.plain_text or '').strip()
            if title and txt.startswith(title[:30]):
                insert_idx = i + 1
                # Advance past any TableNodes already inserted after the title
                while insert_idx < len(active_sub.children) and isinstance(active_sub.children[insert_idx], TableNode):
                    insert_idx += 1
                break
    active_sub.children.insert(insert_idx, tn)
    return True

def inject_tables_into_tree(doc, tables_by_type: dict) -> None:
    """Post-parse: inject TableNodes into sections based on page number.
    
    Each TableNode has a page field. Each SectionNode has a page field.
    We find which section was active when the table appeared and inject
    the table into that section, avoiding false pattern-matching on text.
    """
    from cigna_parse_nodes import SectionNode, SubsectionNode, FootnoteNode, FooterNode

    def _append_node(sec, tn):
        """Insert table before footer content (Cigna Companies disclaimer)."""
        # Find the first child that contains footer text
        insert_idx = len(sec.children)
        for i, child in enumerate(sec.children):
            is_footer = False
            if isinstance(child, (FootnoteNode, FooterNode)):
                if ('Cigna' in child.text or
                        'operating subsidiaries' in child.text or
                        'Cigna Group' in child.text):
                    is_footer = True
            elif isinstance(child, ParagraphBlockNode):
                txt = getattr(child.block, 'plain_text', '') or ''
                if ('Cigna' in txt or
                        'operating subsidiaries' in txt or
                        '© 20' in txt):
                    is_footer = True
            if is_footer:
                insert_idx = i
                break
        sec.children.insert(insert_idx, tn)

    # Collect all table nodes in page order
    all_tables = []
    for ttype in ('revision', 'dql', 'availability', 'criteria',
                  'hcpcs', 'moa', 'generic'):
        for tn in tables_by_type.get(ttype, []):
            all_tables.append(tn)
    # Sort by page
    all_tables.sort(key=lambda t: t.page)

    # Build section page ranges from doc tree
    sections = [n for n in doc.nodes if isinstance(n, SectionNode)]

    def _section_for_page(pg, top=0.0):
        """Return the section active at (page, top)."""
        active = None
        for sec in sections:
            if sec.page < pg:
                active = sec
            elif sec.page == pg and sec.top <= top:
                active = sec
            else:
                break
        return active

    def _inject_dql(section, tn):
        for child in section.children:
            if isinstance(child, SubsectionNode):
                hl = child.heading.lower()
                if 'drug quantity' in hl or 'quantity limit' in hl:
                    # Find insertion point: after any already-inserted TableNodes,
                    # before any ParagraphBlockNodes (footnotes/text follow tables)
                    insert_idx = 0
                    for i, sc in enumerate(child.children):
                        if isinstance(sc, TableNode):
                            insert_idx = i + 1
                        elif isinstance(sc, ParagraphBlockNode):
                            break
                    child.children.insert(insert_idx, tn)
                    return True
        return False

    def _inject_availability(section, tn):
        for child in section.children:
            if isinstance(child, SubsectionNode) and 'availability' in child.heading.lower():
                # Find insertion point: after table title paragraph,
                # but before any FootnoteNodes
                insert_idx = len(child.children)
                found_title = False
                for idx, sc in enumerate(child.children):
                    if isinstance(sc, ParagraphBlockNode):
                        txt = (sc.block.plain_text or '').strip()
                        if tn.title and txt.startswith(tn.title):
                            found_title = True
                            insert_idx = idx + 1
                    elif isinstance(sc, FootnoteNode) and found_title:
                        # Stop here — insert before footnote
                        insert_idx = idx
                        break
                child.children.insert(insert_idx, tn)
                return True
        return False

    def _inject_moa(section_node, tn):
        """Insert MOA table before footnotes at start of section."""
        # Find first FootnoteNode or '*' paragraph — insert before it
        for i, child in enumerate(section_node.children):
            if isinstance(child, FootnoteNode):
                section_node.children.insert(i, tn)
                return
            if isinstance(child, ParagraphBlockNode):
                txt = (child.block.plain_text or '').strip()
                if txt.startswith('*'):
                    section_node.children.insert(i, tn)
                    return
        # No footnote found — prepend
        section_node.children.insert(0, tn)

    def _inject_hcpcs(section_node, tn):
        """Insert HCPCS table after all intro paragraphs,
        before any FootnoteNode or FooterNode."""
        # Find the last ParagraphBlockNode — insert after it
        last_para_idx = -1
        for i, child in enumerate(section_node.children):
            if isinstance(child, ParagraphBlockNode):
                last_para_idx = i
        if last_para_idx >= 0:
            section_node.children.insert(last_para_idx + 1, tn)
        else:
            # No paragraphs yet — find first FootnoteNode and insert before it
            for i, child in enumerate(section_node.children):
                if isinstance(child, (FootnoteNode, FooterNode)):
                    section_node.children.insert(i, tn)
                    return
            section_node.children.append(tn)

    # Special handling for criteria in Coverage/Medical Necessity sections
    def _inject_criteria_smart(section, tn):
        """Inject criteria table after employer/individual plan headings."""
        from reconstruct_cigna_bullet import PlainText as _PT
        for child in section.children:
            if isinstance(child, SubsectionNode):
                hl = child.heading.lower()
                if 'employer' in hl or 'individual' in hl or 'family' in hl:
                    child.children.append(tn)
                    return True
                # Check paragraphs within subsection
                for sc in child.children:
                    if isinstance(sc, ParagraphBlockNode):
                        all_text = sc.block.plain_text or ''
                        for _it in sc.block.items:
                            if isinstance(_it, _PT):
                                all_text += ' ' + _it.text
                        ptxt = all_text.lower()
                        if (len(all_text) < 30 and
                                ('employer plans' in ptxt or
                                 'individual and family' in ptxt or
                                 'individual/family' in ptxt)):
                            idx = child.children.index(sc)
                            child.children.insert(idx + 1, tn)
                            return True
        # Fallback: append to section
        _append_node(section, tn)
        return True

    # Inject each table into its section
    for tn in all_tables:
        section = _section_for_page(tn.page, tn.top)
        if section is None:
            continue

        hl = section.heading.lower()

        if tn.table_type == 'revision':
            if 'revision' in hl:
                section.children.insert(0, tn)
            else:
                _append_node(section, tn)

        elif tn.table_type == 'dql':
            if not _inject_dql(section, tn):
                _append_node(section, tn)

        elif tn.table_type == 'availability':
            if not _inject_availability(section, tn):
                _append_node(section, tn)

        elif tn.table_type == 'hcpcs':
            if 'coding' in hl:
                _inject_hcpcs(section, tn)
            else:
                _append_node(section, tn)

        elif tn.table_type == 'moa':
            _inject_moa(section, tn)
            continue

        elif tn.table_type == 'criteria':
            if 'coverage' in hl or 'medical necessity' in hl:
                _inject_criteria_smart(section, tn)
            else:
                _append_node(section, tn)

        elif tn.table_type == 'generic':
            if not _inject_into_active_subsection(section, tn):
                _append_node(section, tn)

