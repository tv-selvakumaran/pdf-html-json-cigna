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

from cigna_constants import (
    FOOTER_PATTERNS,
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



# ════════════════════════════════════════════════════════════════════════════
# Detecting sections and file families
# ════════════════════════════════════════════════════════════════════════════

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


def _is_mm_family_by_prefix(stem: str) -> bool | None:
    """
    Determine document family purely from filename prefix, using the
    exhaustive, verified mapping covering every file in both the
    medical-administrative (188 files) and drug (640 files) corpora.
    Returns True (mm family), False (drug family), or None if the prefix
    doesn't match any known pattern (caller should fall back to
    policy_type/doc_type bold-text detection in that case).
    """
    s = stem.lower()
 
    # Multi-part / non-underscore-delimited prefixes -- check these first,
    # since a naive split('_')[0] would misidentify them (e.g. 'en_mm_0086'
    # splits to 'en', not 'en_mm'; 'um20-...' has no underscore at all).
    if s.startswith('en_mm'):
        return True
    if s.startswith('hm_cln'):
        return True
    if s.startswith('um'):
        return True
    if s.startswith('cpg'):
        return True
 
    # Single first-token prefixes (underscore-delimited)
    first_token = s.split('_')[0]
    if first_token in ('ad', 'mm'):
        return True
    if first_token in ('dqm', 'ip', 'ph', 'psm', 'st', 'p'):
        return False
 
    return None  # unknown prefix -- fall back to bold-text detection


def _is_cpg_family(stem: str) -> bool:
    """True if this document belongs specifically to the cpg* family
    (a subset of the broader mm family), which uses its own additional
    section vocabulary (Guidelines, Literature Review, Description,
    Documentation Guidelines) not shared with other mm_/ad_/en_/hm_/um*
    documents."""
    return stem.lower().startswith('cpg')


def _is_ph_family(stem: str) -> bool:
    """True if this document belongs specifically to the ph_ family
    (a subset of the broader drug/non-mm family), which uses its own
    additional section vocabulary (General Background, Recommended
    Dosing, FDA Approved Indication(s), FDA Recommended Dosing) not
    shared with other drug-corpus documents (dqm_/ip_/p_/psm_/st_)."""
    return stem.lower().split('_')[0] == 'ph'


# ════════════════════════════════════════════════════════════════════════════
# Detect borderless continuation 
# ════════════════════════════════════════════════════════════════════════════

def _detect_borderless_continuation_table(page):
    """
    Fallback for pages where page.find_tables() finds nothing, but the
    page is actually a borderless mid-cell continuation: three vertical
    lines/rects spanning (near) the full page height, with one
    horizontal line connecting them at the bottom, and text content
    sitting ENTIRELY in the column after the middle divider (nothing in
    the first column) -- confirming the empty-first-column continuation
    shape (a giant table cell spilling from the previous page with no
    row/column structure of its own).
 
    Returns a pdfplumber Table object (constructed via find_tables with
    explicit coordinates) if the pattern matches, else None.
    """
    page_height = page.height
    min_span = page_height * 0.7  # verticals must span at least 70% of page height
 
    verticals = []
    for r in page.rects:
        w = r['x1'] - r['x0']
        h = r['bottom'] - r['top']
        if w < 3 and h >= min_span:
            verticals.append(round((r['x0'] + r['x1']) / 2, 1))
    verticals = sorted(set(verticals))
 
    if len(verticals) < 3:
        return None
 
    v_left, v_right = verticals[0], verticals[-1]
    v_middles = [v for v in verticals if v_left < v < v_right]
    if not v_middles:
        return None
    v_mid = v_middles[0]
 
    bottom_candidates = [
        r for r in page.rects
        if (r['bottom'] - r['top']) < 3 and
           (r['x1'] - r['x0']) > (v_right - v_left) * 0.5 and
           r['top'] > page_height * 0.8
    ]
    if not bottom_candidates:
        return None
    horiz_bottom = max(r['bottom'] for r in bottom_candidates)
 
    vertical_tops = [r['top'] for r in page.rects
                      if (r['x1'] - r['x0']) < 3 and (r['bottom'] - r['top']) >= min_span]
    horiz_top = min(vertical_tops) if vertical_tops else 0
 
    candidate = page.find_tables(table_settings={
        "explicit_vertical_lines": [v_left, v_mid, v_right],
        "explicit_horizontal_lines": [horiz_top, horiz_bottom],
    })
    if not candidate:
        return None
 
    t = candidate[0]
    data = t.extract()
    if not data or len(data) != 1:
        return None  # only handle the simple single-row-blob case for now
 
    row = data[0]
    if len(row) != 2:
        return None
 
    col0 = (row[0] or '').strip()
    col1 = (row[1] or '').strip()
    if col0 or not col1:
        return None  # doesn't match the expected empty-first-column shape
 
    return t


# ════════════════════════════════════════════════════════════════════════════
# Spurious table detection
# ════════════════════════════════════════════════════════════════════════════

def _count_internal_grid_lines(page, bbox, margin=2):
    """
    Count line-like elements STRICTLY INSIDE bbox (not matching its outer
    edges within `margin` points) -- both explicit page.lines and thin
    rects (acting as ruling lines). Returns (n_horizontal, n_vertical).
    A genuine bordered table has internal row/column separators; prose
    mistakenly detected as a table (via pdfplumber's text-alignment
    strategy) typically has none.
    """
    x0, top, x1, bottom = bbox
    n_h, n_v = 0, 0
 
    def is_internal_h(y):
        return (top + margin) < y < (bottom - margin)
 
    def is_internal_v(x):
        return (x0 + margin) < x < (x1 - margin)
 
    for l in page.lines:
        if not (x0 - margin <= l['x0'] <= x1 + margin and top - margin <= l['top'] <= bottom + margin):
            continue
        if abs(l['top'] - l['bottom']) < 1 and is_internal_h(l['top']):
            n_h += 1
        elif abs(l['x0'] - l['x1']) < 1 and is_internal_v(l['x0']):
            n_v += 1
 
    for r in page.rects:
        if not (x0 - margin <= r['x0'] <= x1 + margin and top - margin <= r['top'] <= bottom + margin):
            continue


def _count_internal_grid_lines(page, bbox, margin=2):
    """
    Count line-like elements STRICTLY INSIDE bbox (not matching its outer
    edges within `margin` points) -- both explicit page.lines and thin
    rects (acting as ruling lines). Returns (n_horizontal, n_vertical).
    A genuine bordered table has internal row/column separators; prose
    mistakenly detected as a table (via pdfplumber's text-alignment
    strategy) typically has none.
    """
    x0, top, x1, bottom = bbox
    n_h, n_v = 0, 0
 
    def is_internal_h(y):
        return (top + margin) < y < (bottom - margin)
 
    def is_internal_v(x):
        return (x0 + margin) < x < (x1 - margin)
 
    for l in page.lines:
        if not (x0 - margin <= l['x0'] <= x1 + margin and top - margin <= l['top'] <= bottom + margin):
            continue
        if abs(l['top'] - l['bottom']) < 1 and is_internal_h(l['top']):
            n_h += 1
        elif abs(l['x0'] - l['x1']) < 1 and is_internal_v(l['x0']):
            n_v += 1
 
    for r in page.rects:
        if not (x0 - margin <= r['x0'] <= x1 + margin and top - margin <= r['top'] <= bottom + margin):
            continue
        h = r['bottom'] - r['top']
        w = r['x1'] - r['x0']
        if h < 3 and w > 10 and is_internal_h(r['top']):
            n_h += 1
        elif w < 3 and h > 10 and is_internal_v(r['x0']):
            n_v += 1
 
    return n_h, n_v


def _is_spurious_table(tdata: list, bbox: tuple, page_bboxes: list,
                       page=None, before_ifu=False) -> tuple[bool, str | None]:
    """
    Return True if this pdfplumber table is an artifact, not a real table.

    Spurious tables are:
    - Very narrow (height < 30pt) = horizontal rule line
    - Single-column with all-empty cells = green rule or empty border
    - Single-column strictly contained within another table = sub-cell
    - Contains a green-filled rect (Cigna section heading stripe)
    """
    if not tdata:
        return True, None        # empty tdata -- no specific reason bucket
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
                return True, 'green_stripe'      # NOT in approved 6 -- stays plain 'spurious'

    # Very narrow = horizontal rule
    if height < 30 and cols <= 2:
        if _looks_like_header_of_kind(row0):
            pass  # a short table can legitimately be just a header row
                  # (its data row continues on the next page) -- don't
                  # treat it as spurious purely because it's narrow
        else:
            return True, 'narrow_rule'

    # Single-column checks
    if cols == 1:
        # All cells empty
        all_empty = all(
            not (cell or '').strip()
            for row in tdata for cell in (row or [])
        )
        if all_empty:
            return True, 'empty_single_col'
        # Strictly contained within another bbox on same page
        for ob in page_bboxes:
            if ob is bbox:
                continue
            if (ob[0] < bbox[0] and ob[1] <= bbox[1] and
                    ob[2] > bbox[2] and ob[3] >= bbox[3]):
                return True, 'contained_single_col'

    # Multi-column table strictly contained within another bbox = sub-table artifact
    for ob in page_bboxes:
        if ob is bbox:
            continue
        if (ob[0] <= bbox[0] and ob[1] <= bbox[1] and
                ob[2] >= bbox[2] and ob[3] >= bbox[3] and
                (ob[2]-ob[0]) > (bbox[2]-bbox[0]) * 1.5):
            return True, 'contained_multi_col'

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

        # Exempt scoring-criteria tables: most rows end in "...: +N" / "...: -N"
        # (e.g. diagnostic point-value rubrics) even though they read like
        # long single-column prose.
        _score_suffix_re = re.compile(r':\s*[+-]?\d+\s*$')
        _score_rows = sum(1 for c in non_empty
                           if _score_suffix_re.search(c.split('\n')[-1].strip()))
        _is_scoring_table = bool(non_empty) and _score_rows >= len(non_empty) * 0.5

        if (not _is_scoring_table and
                len(first_cell) > 15 and ' ' in first_cell and
                not any(first_cell.startswith(h) for h in header_starts) and
                (first_cell[0].isupper() or _is_indented) and
                long_rows > len(non_empty) * 0.4):  # majority are long sentences
            return True, 'body_text_paragraph'

    # Single-column with only 1 non-empty row out of many = bordered paragraph
    if cols == 1 and len(tdata) > 3:
        non_empty = sum(1 for row in tdata
                        if any((c or '').strip() for c in (row or [])))
        if non_empty <= 1:
            return True, 'mostly_empty_single_col'           # NOT in approved 6 -- stays plain 'spurious'

    # Bullet list drawn inside a multi-column grid (e.g. psm_* Applicable
    # Products): every populated row has exactly ONE non-empty cell (bullet
    # text in one column, gray sub-group heading in another) and at least
    # one cell is a bullet. Real multi-column tables populate several cells
    # per data row. Rejecting here lets the lines take the paragraph path,
    # which keeps the bullet/sub-bullet hierarchy.
    if before_ifu and cols >= 2 and len(tdata) >= 2:
        _populated_counts = []
        _has_bullet = False
        for row in tdata:
            cells = [(c or '').strip() for c in (row or [])]
            n = sum(1 for c in cells if c)
            if n == 0:
                continue
            _populated_counts.append(n)
            if any(c.startswith(('•', '\u2022')) for c in cells):
                _has_bullet = True
        if _populated_counts and all(n == 1 for n in _populated_counts) and _has_bullet:
            return True, 'bullet_list_grid'

    return False, None


def _is_table_spurious(tdata: list, bbox: tuple, page_bboxes: list,
                      page=None, page_num: int = 0,
                      section_boundaries: list = None) -> tuple[bool, str | None]:
    """Complete spurious table check — structural + section-aware."""
    # Structural check
    is_spurious, reason = _is_spurious_table(tdata, bbox, page_bboxes, page=page)
    if is_spurious:
        return True, reason
    
    # Section-aware checks
    if section_boundaries and page_num:
        sec = _section_at(section_boundaries, page_num, bbox[1])
        sec_lc = sec.lower() if sec else ''
        if 'reference' in sec_lc:
            return True, 'section_references'     #NOT in approved 6 -- stays plain 'spurious'
        if 'general background' in sec_lc:
            if tdata and max((len(row) for row in tdata if row), default=0) == 1:
                return True, 'section_general_background_1col'
    
    return False, None



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


_APPROVED_SPURIOUS_REASONS = {
    'narrow_rule': 'spurious_narrow_rule',
    'empty_single_col': 'spurious_empty_single_col',
    'contained_single_col': 'spurious_contained_single_col',
    'contained_multi_col': 'spurious_contained_multi_col',
    'body_text_paragraph': 'spurious_body_text',
}


_SPURIOUS_REASON_TYPES = {
    'spurious_narrow_rule', 'spurious_empty_single_col',
    'spurious_contained_single_col', 'spurious_contained_multi_col',
    'spurious_body_text', 
}


def _revert_spurious_reasons_for_drug_family(table_info: list) -> None:
    """
    The 6 reason-specific spurious_* types are currently only meant to
    apply to the medical-administrative corpus. Collapse them back to
    plain 'spurious' for drug-family documents until this is revisited.
    """
    for entry in table_info:
        if entry.get('table_type') in _SPURIOUS_REASON_TYPES:
            entry['table_type'] = 'spurious'


def _is_nested_in_sibling(bbox: tuple, page_bboxes: list, tol: float = 0.5) -> bool:
    for ob in page_bboxes:
        if ob is bbox or ob == bbox:
            continue
        if (ob[0] <= bbox[0] + tol and ob[1] <= bbox[1] + tol and
                ob[2] >= bbox[2] - tol and ob[3] >= bbox[3] - tol):
            return True
    return False
 

# ════════════════════════════════════════════════════════════════════════════
# prod_indication_matrix table utilities
# ════════════════════════════════════════════════════════════════════════════

def _row_fill_dominant(row_obj, page, band: float = 12.0):
    if row_obj is None:
        return None
    rtop = row_obj.bbox[1]
    rbot = min(row_obj.bbox[3], rtop + band)

    area_by_color = {}
    for r in page.rects:
        if not r.get('fill'):
            continue
        if (r['x1'] - r['x0']) < 10:
            continue
        overlap_h = min(r['bottom'], rbot) - max(r['top'], rtop)
        if overlap_h <= 0:
            continue
        col = r.get('non_stroking_color', 0)
        if isinstance(col, (list, tuple)):
            col = sum(col) / len(col) if col else 0
        col = round(float(col), 3)
        if col <= 0:
            continue
        area = overlap_h * (r['x1'] - r['x0'])
        area_by_color[col] = area_by_color.get(col, 0) + area
    if not area_by_color:
        return None
    return max(area_by_color.items(), key=lambda kv: kv[1])[0]


# ════════════════════════════════════════════════════════════════════════════
# 3-step_medications table utilities
# ════════════════════════════════════════════════════════════════════════════

_THREE_STEP_TITLE_RE = re.compile(r'cigna employer group plans', re.IGNORECASE)
_THREE_STEP_SKIP_TYPES = {
    'spurious_contained_single_col', 
    'spurious_contained_multi_col',
    'spurious_narrow_rule',
}
 
 
def _apply_three_step_medications_override(table_info: list) -> None:
    _active = False
    for entry in table_info:
        ttype = entry.get('table_type')
        title = entry.get('title', '') or ''
 
        if _THREE_STEP_TITLE_RE.search(title):
            entry['table_type'] = '3-step_medications'
            _active = True
        elif _active and ttype in ('generic', 'generic_continuation'):
            entry['table_type'] = '3-step_medications_continuation'
            # _active stays True
        elif ttype in _THREE_STEP_SKIP_TYPES:
            pass  # nested sub-fragment on the same page -- don't touch,
                  # don't break the lineage either
        else:
            _active = False



# ════════════════════════════════════════════════════════════════════════════
# Dosing table utilities
# ════════════════════════════════════════════════════════════════════════════

_DOSE_TITLE_RE = re.compile(r'\bdos(e|ing|age)\b', re.IGNORECASE)
 
_DOSING_TABLE_ELIGIBLE_TYPES = {
    'generic', 'unknown', 'unknown_continuation', 'generic_continuation',
}


def _apply_dosing_table_override(table_info: list) -> None:
    """
    Coarse override: any table currently sitting in one of the
    'reduction target' buckets (generic/unknown/*_continuation/spurious_*)
    whose title mentions dose/dosing/dosage gets reclassified as
    'dosing_table'. Does not attempt to unify the diverse internal
    structures (2-col no-header, 3-col with header, drug-specific
    content) -- the title signal alone is the classification basis here,
    same coarseness as network_adequacy_criteria.
    """
    for entry in table_info:
        ttype = entry.get('table_type')
        is_eligible = (
            ttype in _DOSING_TABLE_ELIGIBLE_TYPES or
            (isinstance(ttype, str) and ttype.startswith('spurious'))
        )
        if not is_eligible:
            continue
        title = entry.get('title', '') or ''
        if _DOSE_TITLE_RE.search(title):
            entry['table_type'] = 'dosing_table'
 


# ════════════════════════════════════════════════════════════════════════════
# hct recommendations table utilities
# ════════════════════════════════════════════════════════════════════════════

_HCT_CODE_RE = re.compile(r'^[A-Z]\*?$')
# e.g. 'N', 'S', 'C', 'R', 'D', 'S*'
 
 
def _is_hct_header(tdata, tier_labels):
    if not tdata:
        return False
    header_idxs = [i for i, lbl in (tier_labels or {}).items() if lbl == 'header']
    if not header_idxs:
        header_idxs = [0]
 
    header_cells = []
    for i in header_idxs:
        if i < len(tdata):
            for c in (tdata[i] or []):
                t = (c or '').strip().lower()
                if t:
                    header_cells.append(t)
 
    has_allogeneic = any('allogeneic' in c for c in header_cells)
    has_autologous = any('autologous' in c for c in header_cells)
    return has_allogeneic and has_autologous
 
 
def _hct_continuation_match(tdata, threshold=0.5):
    """
    True if, across the LAST TWO non-empty columns (both code columns
    typically carry data), a majority of values match the single-letter
    code format.
    """
    if not tdata:
        return False
    max_cols = max((len(row) for row in tdata if row), default=0)
    if max_cols < 2:
        return False
 
    # collect the last two non-empty column INDICES seen anywhere in tdata
    nonempty_col_idxs = set()
    for row in tdata:
        for ci, c in enumerate(row or []):
            if (c or '').strip():
                nonempty_col_idxs.add(ci)
    if len(nonempty_col_idxs) < 2:
        return False
    last_two_cols = sorted(nonempty_col_idxs)[-2:]
 
    values = []
    for row in tdata:
        for ci in last_two_cols:
            if ci < len(row or []):
                v = (row[ci] or '').strip()
                if v:
                    values.append(v)
    if not values:
        return False
 
    hits = sum(1 for v in values if _HCT_CODE_RE.match(v.split('\n')[0].strip()))
    return (hits / len(values)) >= threshold



# ════════════════════════════════════════════════════════════════════════════
# Indications -- covered, non-covered -- table utilities
# ════════════════════════════════════════════════════════════════════════════

def _indications_variant_from_header(tdata, max_rows=4):
    """
    Returns 'covered', 'non-covered', or None based on banner text found
    in the first few rows of tdata.
    """
    if not tdata:
        return None
    flat = ' '.join(
        (c or '').strip() for row in tdata[:max_rows] for c in (row or [])
    ).lower()
    if 'not covered' in flat:
        return 'non-covered'
    if 'covered indication' in flat:
        return 'covered'
    return None



# ════════════════════════════════════════════════════════════════════════════
# NAC table utilities
# ════════════════════════════════════════════════════════════════════════════

def _apply_network_adequacy_override(table_info: list, section_boundaries: list,
                                     pdf_stem: str) -> None:
    """
    Coarse override: any table currently classified 'generic' that sits
    in the 'Attachments' section of um20 specifically gets reclassified
    as 'network_adequacy_criteria'. Scoped narrowly to um20 (not um35/um41,
    whose own Attachments sections have different, unexamined content).
    Does not attempt to distinguish the internal sub-shapes (NAC tables,
    Ratio Standard tables, the wide Time-and-Distance grid, IFP Policy
    Guidelines) -- section membership alone is the signal here.
    """
    if not pdf_stem.lower().startswith('um20'):
        return
 
    def _sec(pg, top):
        if not section_boundaries:
            return ''
        active = ''
        for b in section_boundaries:
            if b['page'] < pg or (b['page'] == pg and b['top'] <= top):
                active = b['segment']
            else:
                break
        return active
 
    for entry in table_info:
        if entry.get('table_type') != 'generic':
            continue
        sec = _sec(entry['page'], entry['bbox'][1])
        if 'attachments' in sec.lower():
            entry['table_type'] = 'network_adequacy_criteria'
 


# ════════════════════════════════════════════════════════════════════════════
# COR/LOE table utilities
# ════════════════════════════════════════════════════════════════════════════

_COR_LOE_COMPOUND_RE = re.compile(
    r'cor\s*:?\s*(iia|iib|iv|i{1,3}).*loe\s*:?\s*[a-z][-a-z]{0,3}',
    re.IGNORECASE | re.DOTALL
)

_COR_LOE_GRADE_RE = re.compile(r'^(IIa|IIb|IV|I{1,3})[\s/]+[A-Z][-A-Z]{0,3}$')
# e.g. 'I/B-NR', 'IIa/B-NR', 'IIb/B-NR', 'III/B-R', 'IIa C-LD', 'I/C-EO'
# NOTE: 'IIa'/'IIb' must be checked BEFORE the bare 'I{1,3}' alternative,
# since I{1,3} would otherwise greedily match just 'II' and leave the
# trailing 'a'/'b' unconsumed, breaking the match.

_COR_ABBREV_CELL_RE = re.compile(r'^COR\*?$', re.IGNORECASE)
_LOE_ABBREV_CELL_RE = re.compile(r'^LOE\*?$', re.IGNORECASE)

def _is_cor_loe_header(tdata, tier_labels):
    """
    True if the header (tier_labels=='header', with row0 fallback) EITHER:
      (a) contains cells for 'Indication', something containing
          'Recommendation', and something reducing to 'corloe'
          (the original combo check), OR
      (b) contains bare standalone 'COR'/'COR*' AND 'LOE'/'LOE*' columns,
          regardless of what the other header cells say -- this signal
          alone is distinctive enough (confirmed via mm_0129/mm_0469,
          where the first column is a combined title or citation note
          instead of a literal 'Indication' cell).
    """
    if not tdata:
        return False
    header_idxs = [i for i, lbl in (tier_labels or {}).items() if lbl == 'header']
    if not header_idxs:
        header_idxs = [0]
 
    header_cells = []
    for i in header_idxs:
        if i < len(tdata):
            for c in (tdata[i] or []):
                t = (c or '').strip()
                if t:
                    header_cells.append(t)
 
    header_cells_lower = [c.lower() for c in header_cells]
 
    # Condition (a): original combo check
    has_indication = any(c in ('indication', 'indications') for c in header_cells_lower)
    has_recommendation = any('recommendation' in c for c in header_cells_lower)
    has_cor_loe_combined = any(
        re.sub(r'[^a-z]', '', c) == 'corloe' for c in header_cells_lower)
    combo_match = has_indication and has_recommendation and has_cor_loe_combined
 
    # Condition (b): bare COR*/LOE* standalone columns
    has_cor_abbrev = any(_COR_ABBREV_CELL_RE.match(c) for c in header_cells)
    has_loe_abbrev = any(_LOE_ABBREV_CELL_RE.match(c) for c in header_cells)
    bare_match = has_cor_abbrev and has_loe_abbrev
    banner_match = _is_cor_loe_banner_header(tdata, tier_labels)

    return combo_match or bare_match or banner_match


def _cor_loe_continuation_match(tdata, threshold=0.5):
    """
    True if a majority of rows' rightmost non-empty cell matches the
    grade-code format. Used to recognize HEADERLESS continuation
    fragments of an already-active COR/LOE lineage -- these fragments
    have data only, no header, so we confirm via the grade-code column
    instead of any header text.
    """
    if not tdata:
        return False
    last_col_vals = []
    for row in tdata:
        if not row:
            continue
        val = None
        for c in reversed(row):
            if (c or '').strip():
                val = (c or '').strip()
                break
        if val:
            last_col_vals.append(val)
    if not last_col_vals:
        return False
    hits = sum( 1 for v in last_col_vals
               if _COR_LOE_GRADE_RE.match(v.split('\n')[0].strip())
               or _COR_LOE_COMPOUND_RE.search(v) )

    return (hits / len(last_col_vals)) >= threshold


def _is_cor_loe_banner_header(tdata, tier_labels):
    idxs = [i for i, lbl in (tier_labels or {}).items() if lbl in ('header', 'subheader')]
    if not idxs:
        idxs = list(range(min(5, len(tdata))))
    cells = []
    for i in idxs[:6]:
        if i < len(tdata):
            for c in (tdata[i] or []):
                t = (c or '').strip().lower()
                if t:
                    cells.append(t)
    _flat = ' '.join(cells)
    return ('recommendation' in _flat and 'evidence' in _flat and
            ('cor' in _flat or 'loe' in _flat))
 


# ════════════════════════════════════════════════════════════════════════════
# TTE scoring table utilities
# ════════════════════════════════════════════════════════════════════════════

_TTE_SCORE_CELL_RE = re.compile(r'^(\d{1,2}/[A-Z]{1,2}|[A-Z]{1,2}\s*\(\d{1,2}\))$')
 
 
def _is_tte_score_table_content(tdata, threshold=0.3):
    """
    True if ANY column has a good fraction of cells matching the
    digit/letter or letter(digit) score-rating format across data rows.
    Checks every column position (not a fixed index), since column
    count/position varies between table variants (2-col, 3-col with
    contrast, etc.), and works on headerless continuation pages since the
    score format itself is the signal -- no header text needed.
    """
    if not tdata:
        return False
 
    max_cols = max((len(row) for row in tdata if row), default=0)
    if max_cols == 0:
        return False
 
    for col_idx in range(max_cols):
        cells = [
            (row[col_idx] or '').strip()
            for row in tdata
            if row and col_idx < len(row) and (row[col_idx] or '').strip()
        ]
        if not cells:
            continue
        hits = sum(1 for c in cells if _TTE_SCORE_CELL_RE.match(c.split('\n')[0].strip()))
        if hits / len(cells) >= threshold:
            return True
 
    return False


def _tte_score_looks_like_fresh_header(tdata):
    """
    True if tdata's first row has the shape of a genuine title/column-
    header row (>=2 populated columns, the last of which is NOT itself
    a score-code value), rather than a headerless continuation
    fragment or plain data row (whose last populated cell IS a score
    code like '3/R', '4/M').
    """
    if not tdata or not tdata[0]:
        return False
    row0 = tdata[0]
    nonempty = [c for c in row0 if c and str(c).strip()]
    if len(nonempty) < 2:
        return False
    last_cell = nonempty[-1].strip().split('\n')[0].strip()
    return not bool(_TTE_SCORE_CELL_RE.match(last_cell))


# ════════════════════════════════════════════════════════════════════════════
# Cancer guidelines table utilities
# ════════════════════════════════════════════════════════════════════════════

def _classify_cancer_guidelines(tdata, tier_labels, sec):
    """
    Detects the 'cancer_guidelines' table shape: a heading row (gray-
    shaded, tier_labels == 'header') that collapses to the single word
    'Cancer', paired with data rows (tier_labels == 'data') that collapse
    to exactly 2 non-empty columns (disease name, guideline text).
 
    Falls back to treating row0 as an implicit header if tier_labels
    found no 'header' tier at all but row0 itself collapses to the lone
    cell 'cancer' -- covers cases where gray-shading detection missed the
    header row on that particular page.
 
    Returns (is_match: bool, aligned_tdata: list | None). aligned_tdata,
    when not None, is the reconstructed 2-column table -- header row
    ['Cancer', ''] followed by data rows positionally aligned so that
    'Cancer' maps to the FIRST (leftmost / smallest raw column index) of
    the two surviving data columns, not by matching raw column index
    between header and data.
    """
    if 'general background' not in sec or not tdata:
        return False, None
 
    tier_labels = tier_labels or {}
    header_idxs = [i for i, lbl in tier_labels.items() if lbl == 'header']
    data_idxs = [i for i, lbl in tier_labels.items() if lbl == 'data']
 
    if not header_idxs:
        row0_nonempty = [(c or '').strip() for c in (tdata[0] or []) if (c or '').strip()]
        if len(row0_nonempty) == 1 and row0_nonempty[0].lower() == 'cancer':
            header_idxs = [0]
            data_idxs = list(range(1, len(tdata)))
 
    if not header_idxs or not data_idxs:
        return False, None
 
    def _nonempty_cols(indices):
        cols = set()
        for i in indices:
            if i >= len(tdata):
                continue
            for ci, c in enumerate(tdata[i] or []):
                if (c or '').strip():
                    cols.add(ci)
        return cols
    header_cols = sorted(_nonempty_cols(header_idxs))
    data_cols = sorted(_nonempty_cols(data_idxs))
 
    if len(header_cols) != 1 or len(data_cols) != 2:
        return False, None
 
    header_col_idx = header_cols[0]
    header_text = None
    for i in header_idxs:
        if i < len(tdata) and header_col_idx < len(tdata[i] or []):
            c = (tdata[i][header_col_idx] or '').strip()
            if c:
                header_text = c.lower()
 
    if header_text != 'cancer':
        return False, None
 
    left_col, right_col = data_cols[0], data_cols[1]
    aligned_data = []
    for i in data_idxs:
        if i >= len(tdata):
            continue
        row = tdata[i] or []
        left_val = (row[left_col] if left_col < len(row) else '') or ''
        right_val = (row[right_col] if right_col < len(row) else '') or ''
        aligned_data.append([left_val, right_val])
 
    return True, [['Cancer', '']] + aligned_data


def _cancer_guidelines_continuation_match(tdata):
    if not tdata:
        return False
    nonempty_cols = set()
    for row in tdata:
        for ci, c in enumerate(row or []):
            if (c or '').strip():
                nonempty_cols.add(ci)
    return len(nonempty_cols) in (1, 2)
 

# ════════════════════════════════════════════════════════════════════════════
# Product Criteria tables utilities
# ════════════════════════════════════════════════════════════════════════════

def _matches_prod_criteria_lineage(tdata, min_chars=40):
    """
    True if the table has substantial text content, regardless of
    column count -- prod_criteria continuation pages are borderless
    single-content-column tables (often with a leading empty cell),
    same shape as fda_recommended_dosing continuations but kept as a
    separate matcher since the two lineages should be investigated
    and tuned independently.
    """
    if not tdata:
        return False
    rows = [row for row in tdata if row]
    if not rows:
        return False
    total_chars = sum(len((c or '').strip()) for row in rows for c in row)
    return total_chars >= min_chars


# ════════════════════════════════════════════════════════════════════════════
# Criteria Use tables utilities
# ════════════════════════════════════════════════════════════════════════════

def _is_criteria_for_use_header(tdata, tier_labels):
    """
    True if the header (tier_labels=='header', with row0 fallback)
    collapses to EXACTLY 2 non-empty cells, one of which is (stripped,
    lowercased) exactly 'criteria' or 'criteria for use' -- NOT the
    broader pref_criteria combo (non-preferred + exception).
    """
    if not tdata:
        return False
    header_idxs = [i for i, lbl in (tier_labels or {}).items() if lbl == 'header']
    _source = tdata
    if not header_idxs:
        header_idxs = [0]
 
    header_cells = []
    for i in header_idxs:
        if i < len(_source):
            for c in (_source[i] or []):
                t = (c or '').strip().lower()
                if t:
                    header_cells.append(t)
 
    if len(header_cells) != 2:
        return False
    has_criteria_label = any(c in ('criteria', 'criteria for use') for c in header_cells)
    has_nonpref_exception = any('non-preferred' in c or 'exception' in c for c in header_cells)
    return has_criteria_label and not has_nonpref_exception


def _matches_criteria_for_use_lineage(tdata):
    if not tdata:
        return False
    nonempty_cols = set()
    for row in tdata:
        for ci, c in enumerate(row or []):
            if (c or '').strip():
                nonempty_cols.add(ci)
    return len(nonempty_cols) in (1, 2)


# ════════════════════════════════════════════════════════════════════════════
# Criteria Description tables utilities
# ════════════════════════════════════════════════════════════════════════════

_SCORED_BULLET_LINE_RE = re.compile(r'^\u2022.*\(\d{1,2}\)\s*$', re.MULTILINE)
 
 
def _is_criteria_description_table(tdata):
    """
    True if every row has at most 1 non-empty column, AND at least one
    cell contains a bullet line ending in a parenthetical score, e.g.
    '• Palpitations with abnormal ECG (6)' -- the distinctive signature
    of the Appropriate/May Be Appropriate/Rarely Appropriate scored
    criteria-description pattern. Requiring the trailing (N) score
    excludes incidental bullet points buried in unrelated guideline-quote
    paragraphs (e.g. cancer_guidelines content), which don't end their
    bullets with a parenthetical score.
    """
    if not tdata:
        return False
    for row in tdata:
        nonempty = sum(1 for c in (row or []) if (c or '').strip())
        if nonempty > 1:
            return False
    has_scored_bullet = any(
        row and any(_SCORED_BULLET_LINE_RE.search(c or '') for c in row)
        for row in tdata
    )
    return has_scored_bullet


# ════════════════════════════════════════════════════════════════════════════
# Criteria scores tables utilities
# ════════════════════════════════════════════════════════════════════════════

_CRITERIA_SCORE_RE = re.compile(r'^[<>]?\s*\d{1,2}(\s*to\s*\d{1,2})?$', re.IGNORECASE)

def _matches_criteria_lineage(tdata, threshold=0.5):
    """
    True if the majority of rows' RIGHTMOST non-empty cell is a small
    integer (the 'Score' value), mirroring how cor_loe_recommendation
    checks its rightmost grade-code column.
    """
    if not tdata:
        return False
    values = []
    for row in tdata:
        if not row:
            continue
        val = None
        for c in reversed(row):
            if (c or '').strip():
                val = (c or '').strip()
                break
        if val:
            values.append(val)
    if not values:
        return False
    hits = sum(1 for v in values if _CRITERIA_SCORE_RE.match(v.split('\n')[0].strip()))
    return (hits / len(values)) >= threshold


# ════════════════════════════════════════════════════════════════════════════
# Transition of Care (toc) tables utilities
# ════════════════════════════════════════════════════════════════════════════

def _apply_toc_table_override(table_info: list, section_boundaries: list,
                               pdf_stem: str) -> None:
    """
    Coarse override: any table currently classified 'generic' that sits
    in the 'Standard Procedure' section of um35/um41 specifically gets
    reclassified as 'transition_of_care'. Confirmed unique to these
    2 documents (9 tables total).
    """
    _stem_lower = pdf_stem.lower()
    if not (_stem_lower.startswith('um35') or _stem_lower.startswith('um41')):
        return
 
    def _sec(pg, top):
        if not section_boundaries:
            return ''
        active = ''
        for b in section_boundaries:
            if b['page'] < pg or (b['page'] == pg and b['top'] <= top):
                active = b['segment']
            else:
                break
        return active
 
    for entry in table_info:
        ttype = entry.get('table_type')
        if ttype not in ('generic', 'generic_continuation'):
            continue
        sec = _sec(entry['page'], entry['bbox'][1])
        if 'standard procedure' in sec.lower():
            entry['table_type'] = (
                'transition_of_care_table' if ttype == 'generic'
                else 'transition_of_care_table_continuation'
            )


# ════════════════════════════════════════════════════════════════════════════
# DQL Lineage tables utilities
# ════════════════════════════════════════════════════════════════════════════

def _matches_dql_lineage(tdata, threshold=0.6):
    """
    True if the majority of rows have at least 2 non-empty cells
    ANYWHERE (not requiring the first cell specifically) -- looser than
    the appendix_med/moa structural check, to accommodate deeply nested
    sub-bullet continuation rows where content sits in a later column.
    """
    if not tdata:
        return False
    rows = [row for row in tdata if row]
    if not rows:
        return False
    matches = 0
    for row in rows:
        nonempty = sum(1 for c in row if (c or '').strip())
        if nonempty >= 2:
            matches += 1
    return (matches / len(rows)) >= threshold



# ════════════════════════════════════════════════════════════════════════════
# FDA Recommended Dosing Lineage tables utilities
# ════════════════════════════════════════════════════════════════════════════

def _matches_fda_dosing_lineage(tdata, min_chars=40):
    """
    True if the table has substantial text content, regardless of
    column count -- fda_recommended_dosing continuation pages are
    borderless single-content-column tables (often with a leading
    empty cell), unlike dql's multi-column-per-row shape.
    """
    if not tdata:
        return False
    rows = [row for row in tdata if row]
    if not rows:
        return False
    total_chars = sum(len((c or '').strip()) for row in rows for c in row)
    return total_chars >= min_chars



# ════════════════════════════════════════════════════════════════════════════
# Coding tables (icd/cpt/hcpcs) utilities
# ════════════════════════════════════════════════════════════════════════════

_ICD10_SINGLE_RE = re.compile(r'^[A-TV-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$')
# e.g. 'G89.28', 'M47.892', 'S29.012D', 'M50.320', 'M79.7', 'S13.4XXA', 'G44.201'
 
_CPT_SINGLE_RE = re.compile(r'^[0-9]{4,5}[A-Z]?$')
# e.g. '95907', '0102T'
 
_HCPCS_SINGLE_RE = re.compile(r'^[A-Z][0-9]{4}$')
# e.g. 'E2301', 'K0001'
 
_CODE_KIND_PATTERNS = {
    'icd': _ICD10_SINGLE_RE,
    'cpt': _CPT_SINGLE_RE,
    'hcpcs': _HCPCS_SINGLE_RE,
}
 
_RANGE_SPLIT_RE = re.compile(r'[-\u2013]')

def _first_code_token(cell_text):
    """
    Normalize a cell's text and return just the FIRST code token, i.e.
    everything before the first hyphen/en-dash (range indicator). Handles
    multi-line cells (join with space) and any range formatting variant,
    since we only need the leading code to be well-formed.
    """
    text = (cell_text or '').replace('\n', ' ').strip()
    if not text:
        return ''
    first_part = _RANGE_SPLIT_RE.split(text, maxsplit=1)[0]
    return first_part.strip()
 

def _looks_like_header_of_kind(row0):
    if not row0 or len(row0) < 2:
        return None
    c0 = (row0[0] or '').strip()
    c1 = (row0[1] or '').strip()
    if c1 == 'Description':
        if 'ICD-10' in c0:
            return 'icd'
        if 'HCPCS' in c0:
            return 'hcpcs'
        if 'CPT' in c0:
            return 'cpt'

    # falls through to the flattened check below if none matched
    # appendix_med / moa: header text sits at variable positions due to
    # padding, so check the whole flattened row instead of fixed c0/c1.
    _flat = ' '.join((c or '') for c in row0).lower()
    if 'medication' in _flat and 'mode of administration' in _flat:
        return 'appendix_med'

    if 'mechanism of action' in _flat and 'indications' in _flat:
        return 'moa'

    _cells_stripped = [(c or '').strip().lower() for c in row0]
    if 'criteria' in _cells_stripped and 'score' in _cells_stripped:
        return 'criteria'

    if 'product' in _cells_stripped and 'criteria' in _cells_stripped:
        _nonempty_cells = [c for c in _cells_stripped if c]
        if set(_nonempty_cells) == {'product', 'criteria'}:
            return 'prod_criteria'  # bare 2-column Product/Criteria header
        return 'dql'

    _cells_stripped = [(c or '').strip().lower() for c in row0]
    if 'procedure' in _cells_stripped and any('cpt' in c for c in _cells_stripped):
        return 'proc_code_policy'

    # 'criteria_for_use' table type
    _row0_nonempty = [(c or '').strip().lower() for c in row0 if (c or '').strip()]
    if len(_row0_nonempty) == 2:
        _has_criteria_label = any(c in ('criteria', 'criteria for use') for c in _row0_nonempty)
        _has_nonpref_exception = any('non-preferred' in c or 'exception' in c for c in _row0_nonempty)
        if _has_criteria_label and not _has_nonpref_exception:
            return 'criteria_for_use'

    return None
 
 
_STRUCTURAL_LINEAGE_MIN_COLS = {
    'appendix_med': 2,
    'moa': 2,   # moa's real content is ~3 cols, but padding varies --
                # require at least 2 non-empty cells as a looser floor
}
 
 
def _matches_code_lineage(tdata, kind, threshold=0.6):
    """
    For icd/cpt/hcpcs: True if the majority of non-empty first-column
    cells match the kind's single-code regex pattern (unchanged from
    before).
 
    For appendix_med/moa: no clean regex code format exists (these are
    drug names, not codes), so fall back to a STRUCTURAL check instead --
    True if the majority of rows have a non-empty first cell (a drug/
    product name) AND at least the expected minimum number of non-empty
    cells overall (confirming it looks like real data, not an empty or
    single-column fragment).
    """
    if not tdata:
        return False
 
    if kind == 'prod_criteria':
        return _matches_prod_criteria_lineage(tdata)

    if kind == 'criteria':
        return _matches_criteria_lineage(tdata)

    if kind == 'dql':
        return _matches_dql_lineage(tdata)

    if kind == 'proc_code_policy':
        return _matches_dql_lineage(tdata)   # reuse the existing looser check

    if kind == 'criteria_for_use':
        return _matches_criteria_for_use_lineage(tdata)

    pat = _CODE_KIND_PATTERNS.get(kind)
    if pat is not None:
        first_col = [(row[0] or '').strip() for row in tdata if row and (row[0] or '').strip()]
        if not first_col:
            return False
        hits = sum(1 for c in first_col if pat.match(_first_code_token(c)))
        return (hits / len(first_col)) >= threshold
 
    min_cols = _STRUCTURAL_LINEAGE_MIN_COLS.get(kind)
    if min_cols is not None:
        rows = [row for row in tdata if row]
        if not rows:
            return False
        matches = 0
        for row in rows:
            nonempty = sum(1 for c in row if (c or '').strip())
            first_cell = (row[0] or '').strip()
            if nonempty >= min_cols and first_cell:
                matches += 1
        return (matches / len(rows)) >= threshold
 
    return False


# ════════════════════════════════════════════════════════════════════════════
# Pre-processing utilities in find_table_info
# ════════════════════════════════════════════════════════════════════════════

def _find_ifu_pos(para_cache):
    """(page, top) of the IFU/Purpose heading, or None."""
    for pg in sorted(para_cache):
        for l in para_cache[pg]:
            t = l['text'].strip().lower()
            if l.get('bold') and ('instructions for use' in t or t == 'purpose'):
                return (pg, l['top'])
    return None


def _fontname_base(fn):
    if not fn:
        return fn
    return re.sub(r'-(Italic|Bold|BoldItalic|Oblique)$', '', fn, flags=re.IGNORECASE)


def _fontname_family(fn):
    """Extract the base font family, ignoring PDF subset prefix
    (e.g. 'XUMOAZ+') and style suffix (Italic/Bold/BoldItalic), so
    'XUMOAZ+Verdana' and 'DGIOGN+Verdana-Italic' both normalize to
    'Verdana' for continuation-matching purposes."""
    if not fn:
        return fn
    _base = fn.split('+', 1)[-1]  # strip subset prefix
    _base = re.sub(r'-(Italic|Bold|BoldItalic|Oblique)$', '', _base, flags=re.IGNORECASE)
    return _base



# ════════════════════════════════════════════════════════════════════════════
# Table info (replaces find_table_bboxes)
# ════════════════════════════════════════════════════════════════════════════

def find_table_info(_pdf: PDF, para_cache: dict = None) -> tuple:
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
    prev_row0 = None    
    prev_ttype = None
    code_lineage_kind = None  
    _two_col_heading_seen_data = False
    _spurious_narrow_rule_bboxes = {}  # (pg, bbox) -> (en_top, en_bottom) candidate pairs

    pages = _pdf.pages
    for pg, page in enumerate(pages, 1):
        tables = page.find_tables()
        if not tables:
            _fallback = _detect_borderless_continuation_table(page)
            if _fallback is not None:
                tables = [_fallback]
            else:
                prev_bbox = None
                prev_ttype = None
                code_lineage_kind = None
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
            raw_bbox = t.bbox
            bbox = raw_bbox

            tdata, bbox = _recover_borderless_first_row(t, tdata, page, bbox)
            row0 = tdata[0] if tdata else []

            # Skip spurious tables — don't update prev_bbox
            _ifu_pos = _find_ifu_pos(para_cache)
            before_ifu=(_ifu_pos is not None and (pg, bbox[1]) < _ifu_pos)
            _is_spur,  _spur_reason = _is_spurious_table(tdata, bbox, page_bboxes, page=page, before_ifu=before_ifu)

            if _is_spur and _spur_reason != 'narrow_rule':
                ttype = _APPROVED_SPURIOUS_REASONS.get(_spur_reason, 'spurious')
                _cons, _tier_labels = _consolidate_pref_criteria_table(t, tdata, page)
                results.append({
                    'page': pg,
                    'bbox': tuple(round(x, 1) for x in bbox),
                    'raw_bbox': tuple(round(x, 1) for x in bbox),
                    'tdata': tdata,
                    'consolidated_tdata': _cons,
                    'tier_labels': _tier_labels,
                    'table_type': ttype,
                    'title': '',
                    'row0': str([str(c)[:20] if c else None for c in row0[:5]]),
                    'rows': len(tdata),
                    'cols': len(row0),
                    'is_continuation': False,
                    'covered': False,
                })

                _has_active_lineage = (
                    prev_ttype in ('revision', 'prod_criteria', 'tte_score',
                                    'hcpcs', 'cpt', 'icd', 'pref_criteria', 'criteria',
                                    'medicare_coverage_determination', 'moa', 'appendix_med', 
                                    'fda_device_mfg', 'prod_indications_matrix', ) or 
                    (prev_ttype or '').endswith('_continuation')
                )
                if _has_active_lineage:
                    pass  # preserve prev_bbox/prev_row0/prev_ttype
                          # entirely through this spurious interruption
                else:
                    prev_bbox = bbox
                    prev_row0 = tdata[0] if tdata else []
                    prev_ttype = 'spurious'
                continue
            elif _is_spur:
                # narrow_rule: let execution continue through the full
                # loop body (title/footnote/covered_bboxes computation),
                # same as a real table, since this category can hide a
                # genuine code-table continuation row that gets rescued
                # later. Preserve the spurious classification for now --
                # _rescue_hcpcs_cpt_icd_continuations will reclassify it
                # if it matches an active code lineage; if not rescued,
                # it stays spurious but now also has covered_bboxes set,
                # meaning un-rescued narrow_rule content will no longer
                # appear as paragraph text either way.
                ttype = _APPROVED_SPURIOUS_REASONS.get(_spur_reason, 'spurious')

            # Classify
            # fda_recommended_dosing table detection
            is_fda_recommended_dosing = False
            _nonempty_idxs_frd = [i for i, c in enumerate(row0) if (c or '').strip()]
            for _pos, _i in enumerate(_nonempty_idxs_frd[:2]):
                _c = (row0[_i] or '').strip()
                if _c in ('Product', 'Product Name'):
                    _next_i = _nonempty_idxs_frd[_pos + 1] if _pos + 1 < len(_nonempty_idxs_frd) else None
                    _next_val = (row0[_next_i] or '').strip().lower() if _next_i is not None else ''
                    if 'fda recommended dosing' in _next_val or 'recommended dosing' in _next_val:
                        is_fda_recommended_dosing = True
                    break

            # Drug Quantity Limits (DQL) table detection
            has_product = False
            _nonempty_idxs = [i for i, c in enumerate(row0) if (c or '').strip()]
            for _pos, _i in enumerate(_nonempty_idxs[:2]):
                _c = (row0[_i] or '').strip()
                if _c in ('Product', 'Product Name'):
                    _next_i = _nonempty_idxs[_pos + 1] if _pos + 1 < len(_nonempty_idxs) else None
                    _next_val = (row0[_next_i] or '').strip().lower() if _next_i is not None else ''
                    if ('criteria for use' not in _next_val and 
                            'indication' not in _next_val and
                            'criteria' not in _next_val):
                        has_product = True
                    break

            # Product/indications matrix detection (two-level column
            # headers, second-level only for now -- see
            # _consolidate_matrix_table)
            is_prod_indications_matrix = False
            _matrix_cons = None 
            _header2_idxs = []
            _data_idxs = []
            if len(tdata) >= 3:
                _matrix_cons_raw, _matrix_tier_labels = _consolidate_matrix_table(t, tdata, page)
                _header2_idxs = [i for i, lbl in _matrix_tier_labels.items() if lbl == 'header2']
                _data_idxs = [i for i, lbl in _matrix_tier_labels.items() if lbl == 'data']
                if _header2_idxs and _data_idxs:
                    _joined_header2_flat = ' '.join(
                        str(c).strip().lower()
                        for i in _header2_idxs for c in (tdata[i] or []) if c
                    )
                    _is_criteria_like_header = (
                        'criteria' in _joined_header2_flat or
                        'exception' in _joined_header2_flat
                    )
                    _consolidated_header2_row = (
                        _matrix_cons_raw[1] if _matrix_cons_raw and len(_matrix_cons_raw) > 1 else []
                    )
                    _header2_col_count = len([
                        c for c in _consolidated_header2_row if c and str(c).strip()
                    ])
                    _has_enough_columns = _header2_col_count > 2
                    if not _is_criteria_like_header and _has_enough_columns:
                        is_prod_indications_matrix = True
                        _matrix_cons = _matrix_cons_raw

            # Revision and Availability table detection
            is_rev  = bool(row0 and (row0[0] or '').strip() == 'Type of Revision')
            is_avail = tiny_cols >= 4 and not has_product and not (
                _header2_idxs and _data_idxs)

            # Product/Criteria table detection (bare "Criteria" heading,
            # distinct from "Criteria for Use" and from DQL's dosage/limit
            # tables -- these are narrative medical-necessity criteria)
            is_prod_criteria = False
            if _nonempty_idxs and len(_nonempty_idxs) >= 2:
                _c0 = (row0[_nonempty_idxs[0]] or '').strip()
                _c1 = (row0[_nonempty_idxs[1]] or '').strip().lower()
                if _c0 in ('Product', 'Product Name') and _c1 == 'criteria':
                    is_prod_criteria = True

            # Preference/exception criteria table detection
            is_pref_criteria = False
            _pref_cons = None
            _pref_criteria_needs_title_check = False
            if len(tdata) >= 2:
                _cons, _tier_labels = _consolidate_pref_criteria_table(t, tdata, page)
                _header_idxs = [i for i, lbl in _tier_labels.items() if lbl == 'header']
                if _header_idxs and any(lbl == 'data' for lbl in _tier_labels.values()):
                    _header_rows = [_cons[i] for i in sorted(_header_idxs)]
                    n_hcols = max((len(r) for r in _header_rows), default=0)
                    _joined_header = []
                    for c in range(n_hcols):
                        parts = [str((_header_rows[r][c] or '')).strip()
                                 for r in range(len(_header_rows))
                                 if c < len(_header_rows[r]) and (_header_rows[r][c] or '').strip()]
                        _joined_header.append(' '.join(parts))
                    _jh_flat = ' '.join(_joined_header).lower()

                    if (('non-preferred' in _jh_flat or
                        ('non' in _jh_flat and 'preferred' in _jh_flat)) and
                            'exception' in _jh_flat):
                        is_pref_criteria = True
                        _pref_cons = _cons
                    elif 'exception' in _jh_flat:
                        # Missing the non-preferred qualifier in the
                        # header itself -- title (computed later in this
                        # loop) might supply it instead.
                        _pref_criteria_needs_title_check = True

            # Continuation: same left x-bound as last REAL table, near top of page
            # Right x-bound may differ if column count changes across pages
            _visual_top = _true_table_top(bbox, page.rects)
            is_cont = (prev_bbox is not None and
                       abs(bbox[0] - prev_bbox[0]) < 5 and
                       abs(bbox[2] - prev_bbox[2]) < 30 and
                       _visual_top < 120)

            # Is this table's OWN row0 a fresh code-table header?
            _fresh_header_kind = _looks_like_header_of_kind(row0)

            if _fresh_header_kind:
                # A genuinely NEW header (different kind than the active
                # lineage) is never itself a continuation. But if this
                # header simply REPEATS the header of the currently
                # active lineage (e.g. a code table re-printing its
                # column headers on a new page), it IS the continuation.
                if _fresh_header_kind == code_lineage_kind and is_cont:
                    pass  # repeated header of the active lineage -- stays a continuation
                else:
                    is_cont = False
                    if not _is_nested_in_sibling(bbox, page_bboxes):
                        code_lineage_kind = _fresh_header_kind
            else:
                _is_revision_lineage = (prev_ttype == 'revision' or
                                         prev_ttype == 'unknown' or
                                         (prev_ttype or '').endswith('_continuation'))

                if is_cont and code_lineage_kind is not None and \
                        _matches_code_lineage(tdata, code_lineage_kind):
                    # Confirmed continuation of the active code lineage via
                    # first-column code-pattern matching -- no text
                    # comparison to prev_row0 needed at all.
                    pass  # is_cont stays True
                # Additional check: row0 must match previous table's row0
                elif is_cont and prev_row0 is not None and not _is_revision_lineage:
                    _cur_row0 = [str(c or '').strip() for c in (tdata[0] or [])[:2]]
                    _prev_row0 = [str(c or '').strip() for c in prev_row0[:2]]
                    if _cur_row0 != _prev_row0:
                        is_cont = False

                # If the geometric/lineage chain didn't hold, clear the
                # sticky lineage -- it's genuinely broken.
                if not is_cont:
                    if not _is_nested_in_sibling(bbox, page_bboxes):
                        code_lineage_kind = None

            # Additional check: no text rows in between table and continuation
            if is_cont and para_cache is not None:
                _prev_pg_lines = para_cache.get(pg - 1, [])
                _content_between = [
                    l for l in _prev_pg_lines
                    if l['top'] > prev_bbox[3] + 5 
                ]
                if not _content_between:
                    _cur_pg_lines = para_cache.get(pg, [])
                    _content_between = [
                        l for l in _cur_pg_lines
                        if l['top'] < bbox[1] - 5
                    ]
                if _content_between:
                    is_cont = False

            # Title: bold line immediately above table
            # Use para_cache if available (more accurate than pdfplumber words)
            table_top = bbox[1]
            if para_cache is not None:
                _t_title = ''
                _collected = []
                _max_title_lines = 2
                page_lines = para_cache.get(pg, [])
                # Walk backwards to collect consecutive bold lines
                # immediately above the table (handles 2-line titles).
                # Stop at a non-bold line or gap > 14pt.
                _above = sorted(
                    [l for l in page_lines if l['top'] < table_top],
                    key=lambda l: -l['top'])  # nearest first
                _prev_top = table_top
                for _l in _above:
                    if _prev_top - _l['top'] > 20:  # gap > one line
                        break
                    if _l.get('bold', False):
                        _collected.append(_l)
                        _prev_top = _l['top']
                        if len(_collected) >= _max_title_lines:    # <-- max 2 title lines
                            break
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
                title = ' '.join(l['text'] for l in title_lines)[:120]
                # If no title found and table is near top of page,
                # check bottom of previous page for bold title lines
                if not title and table_top < 120 and pg > 1:
                    prev_lines = para_cache.get(pg - 1, [])
                    _prev_bold = [
                        l for l in prev_lines
                        if l.get('bold') and l['top'] > 650 and
                        not any(
                            sp <= pg - 1 <= ep and
                            ((sp == ep and st <= l['top'] <= eb) or
                             (sp != ep and ((pg - 1 == sp and l['top'] >= st) or
                                            (pg - 1 == ep and l['top'] <= eb))))
                            for sp, st, en_st, ep, eb, en_eb in covered_bboxes
                        )
                    ]
                    if _prev_bold:
                        _prev_bold.sort(key=lambda l: l['top'])
                        title = ' '.join(l['text'] for l in _prev_bold)[:120]
                        _title_on_prev_page = pg - 1
                        _title_prev_page_top = min(l['top'] for l in _prev_bold)
                        _title_prev_page_bottom = max(
                            l['top'] + l.get('size', 9.0) for l in _prev_bold)
                    else:
                        _title_on_prev_page = None
                else:
                    _title_on_prev_page = None
            else:
                title_words = [w for w in words
                               if table_top - 16 <= w['top'] < table_top
                               and 'Bold' in w.get('fontname', '')]
                title = ' '.join(w['text'] for w in title_words)[:120]

            # Footnote: small-font non-bold lines immediately below table
            _t_footnote = ''
            if para_cache is not None:
                page_lines = para_cache.get(pg, [])
                table_bot = bbox[3]
                _below = sorted(
                    [l for l in page_lines if l['top'] > table_bot],
                    key=lambda l: l['top'])  # nearest first
                _fn_lines_objs = []  # store line objects for coordinate tracking
                _fn_lines = []
                _prev_bot = table_bot
                for _l in _below:
                    if _l['top'] - _prev_bot > 35:
                        break
                    _size = _l.get('size', 10.0)
                    _text = _l['text'].strip()

                    # Stop unconditionally if this line is clearly a
                    # section heading by font size (e.g. a green-banner
                    # heading like "Background"), regardless of gap
                    # tolerance or continuation state -- a real footnote
                    # line is never this large.
                    if _size >= 12.0:
                        break

                    # Stop if this line is inside another table bbox on this page
                    if any(pb[1] <= _l['top'] <= pb[3] for pb in page_bboxes):
                        break
                    
                    # Stop if large gap from previous footnote line
                    if _fn_lines_objs:
                        _gap = _l['top'] - _fn_lines_objs[-1]['top']
                        _looks_like_new_fn_entry = bool(
                            re.match(r'^[A-Z]{2,5}\s*[–\-]', _text) or
                            _text.startswith('*') or
                            _text.startswith('†') or
                            _text.startswith('‡') or
                            _text.startswith('¥') or
                            _text.startswith('Ω'))
                        _effective_gap_threshold = 26.0 if _looks_like_new_fn_entry else 18.0
                        if _gap > _effective_gap_threshold:
                            break

                    _is_table_title_start = bool(re.match(r'^(Appendix\s+)?Table\s+\d+[.:]', _text))
                    if _l.get('bold', False) and _is_table_title_start:
                        break

                    _is_symbol_note = bool(re.match(r'^[†\*‡]+\s*\w', _text))
                    _is_continuation = bool(_fn_lines_objs)

                    # Bold lines stop detection UNLESS they're symbol notes or continuation
                    if _l.get('bold', False) and not _is_symbol_note and not _is_continuation:
                        break
                    # Large font stops detection UNLESS symbol note or continuation
                    if _size > 10.5 and not _is_symbol_note and not _is_continuation:
                        break

                    # Match footnote pattern OR continuation of previous footnote
                    _is_fn_start = (
                        re.search(r'^[A-Za-z]{1,8}\s*[–\-]\s+\w', _text) or
                        _text.startswith('*') or
                        _text.startswith('†') or
                        _text.startswith('‡') or
                        _text.startswith('¥') or
                        _text.startswith('Ω') or
                        re.match(r'^[A-Z]{2,5}\s*[–\-]', _text) or 
                        re.match(r'^Note\s*:', _text, re.IGNORECASE) or
                        _size <= 8.5)
                    
                    if _is_fn_start or _is_continuation:
                        _fn_lines.append(_text)
                        _fn_lines_objs.append(_l)
                        _prev_bot = _l['top'] + _size
                    else:
                        break
                _t_footnote = ' '.join(_fn_lines)

                # Determine whether the same-page footnote was cut off
                # mid-line (page bottom reached before a natural end),
                # vs. ending naturally -- and capture its size/fontname
                # for matching against next-page continuation candidates.
                _last_line_text = _fn_lines[-1].strip() if _fn_lines else ''
                _last_fn_bottom = (_fn_lines_objs[-1]['top'] + _fn_lines_objs[-1].get('size', 10.0)
                                    if _fn_lines_objs else table_bot)
                _has_content_after_footnote_same_page = any(
                    l['top'] > _last_fn_bottom + 5 and
                    l.get('text', '').strip() and
                    not any(p.match(l['text'].strip()) for p in FOOTER_PATTERNS)
                    for l in page_lines
                )
                _footnote_seems_cut_off = (
                    bool(_last_line_text) and
                    not _last_line_text.rstrip().endswith(('.', ';')) and
                    not _has_content_after_footnote_same_page
                )

                _fn_size = _fn_lines_objs[-1].get('size', 10.0) if _fn_lines_objs else None
                _fn_fontname = _fn_lines_objs[-1].get('fontname') if _fn_lines_objs else None
                
                # Check next page top for footnote continuation
                # (when table ends near bottom of page)
                _table_near_page_bottom = bbox[3] > 640  # heuristic: table ends near bottom of page
                _next_pg_lines = sorted(
                    [l for l in para_cache.get(pg + 1, [])
                     if l.get('size', 10.0) <= 10.0],
                    key=lambda l: l['top'])
                _cont_fn_lines = []
                _cont_fn_objs = []
                _cont_prev_bot = 0.0
                for _l in _next_pg_lines:
                    top = _l['top']
                    _size = _l.get('size', 10.0)
                    _text = _l['text'].strip()
                    _fontname = _l.get('fontname')

                    _is_bold = _l.get('bold', False)
                    if not _cont_fn_objs:
                        if top > 150:
                            break
                        if _fn_lines_objs:
                            # Same-page footnote existed -- only continue
                            # if it looked cut off, matched by size/font.
                            _is_fn = (
                                _footnote_seems_cut_off and
                                _fn_size is not None and
                                abs(_size - _fn_size) < 0.5 and
                                (_fn_fontname is None or
                                 _fontname_family(_l.get('fontname') ) == _fontname_family(_fn_fontname) )
                            )
                        else:
                            # No same-page footnote at all -- only look
                            # for one on the next page if the table ended
                            # near the bottom of its own page (no room
                            # left for a footnote there), and the
                            # candidate line itself looks like a genuine
                            # footnote start (symbol/pattern), not just
                            # any small-font text.
                            _is_symbol_note_next = bool(re.match(r'^[†\*‡]+\s*Note', _text))
                            _looks_like_fn_start = bool(
                                re.search(r'^[A-Za-z]{1,8}\s*[–\-]\s+\w', _text) or
                                _text.startswith('*') or
                                _text.startswith('†') or
                                _text.startswith('‡') or
                                _text.startswith('¥') or
                                _text.startswith('Ω') or
                                re.match(r'^[A-Z]{2,5}\s+[–\-]\s+\w', _text))
                            _is_fn = (_table_near_page_bottom and
                                      _looks_like_fn_start)
                    else:
                        # Already inside a continuation -- keep going
                        # until a structural break: vertical gap, font
                        # change, or size change.
                        _gap = top - _cont_fn_objs[-1]['top']
                        _last_size = _cont_fn_objs[-1].get('size', 10.0)
                        _size_changed = abs(_size - _last_size) >= 1.0
                        _no_structural_break = not (_gap > 18.0 or _size_changed)

                        _matches_fn_pattern = bool(
                            re.search(r'^[A-Za-z]{1,8}\s*[–\-]\s+\w', _text) or
                            _text.startswith('*') or
                            _text.startswith('†') or
                            _text.startswith('‡') or
                            _text.startswith('¥') or
                            _text.startswith('Ω') or
                            re.match(r'^[A-Z]{2,5}\s+[–\-]\s+\w', _text) or
                            _size <= 8.5)

                        _is_fn = _no_structural_break or _matches_fn_pattern

                    if _is_fn:
                        _cont_fn_lines.append(_text)
                        _cont_fn_objs.append(_l)
                        _cont_prev_bot = top + _size
                    else:
                        break
                
                if _cont_fn_lines:
                    _t_footnote = (_t_footnote + ' ' + 
                                   ' '.join(_cont_fn_lines)).strip()

            # Second chance for is_pref_criteria: title may supply the
            # 'non-preferred' qualifier that's missing from the header
            # row itself (title is now computed, unlike where the base
            # is_pref_criteria check originally ran).
            if _pref_criteria_needs_title_check:
                _title_norm_flat = normalize_heading(title or '').lower()
                if 'non' in _title_norm_flat and 'preferred' in _title_norm_flat:
                    is_pref_criteria = True
                    _pref_cons = _cons

            # Determine type
            if _is_spur and _spur_reason == 'narrow_rule':
                ttype = 'spurious_narrow_rule'  # preserve through fall-through processing
            elif is_cont:
                _base_ttype = re.sub(r'(_continuation)+$', '', prev_ttype) if prev_ttype else None
                ttype = f'{_base_ttype}_continuation' if prev_ttype else 'continuation'
            elif is_rev:
                ttype = 'revision'
            elif is_fda_recommended_dosing:
                ttype = 'fda_recommended_dosing'
            elif is_prod_criteria:
                ttype = 'prod_criteria'
            elif has_product:
                ttype = 'dql'
            elif is_pref_criteria:
                ttype = 'pref_criteria'
                tdata = _pref_cons
            elif is_avail:
                ttype = 'availability'
            else:
                ttype = 'unknown'

            covered = True
            if covered:
                # Compute en_top from title lines
                if title and _collected:
                    _en_top = min(l['top'] for l in _collected)
                else:
                    _en_top = bbox[1]
                
                # Compute en_bot from footnote lines
                if _t_footnote and _fn_lines_objs:
                    _last_fn = _fn_lines_objs[-1]
                    _en_bot = _last_fn['top'] + _last_fn.get('size', 9.0)
                else:
                    _en_bot = bbox[3]

            if _is_spur and _spur_reason == 'narrow_rule':
                _spurious_narrow_rule_bboxes[(pg, tuple(round(x, 1) for x in bbox))] = (_en_top, _en_bot)

            # Detect gray_bottom — bottom of last gray-filled rect within table bbox
            # Used to identify header rows (gray background)
            _gray_bottom = None
            for _r in page.rects:
                _col = _r.get('non_stroking_color', 0)
                if isinstance(_col, (list, tuple)):
                    _col = sum(_col) / len(_col) if _col else 0
                if (_r.get('fill') and 0.3 <= float(_col) <= 0.98 and
                        _r['top'] >= bbox[1] - 2 and
                        _r['bottom'] <= bbox[3] + 2):
                    if _gray_bottom is None or _r['bottom'] > _gray_bottom:
                        _gray_bottom = _r['bottom']

            # identify pref_criteria table type
            _left_words = ' '.join(
                w['text'] for w in words
                if w['x1'] <= bbox[0]+10 and 
                bbox[1]-5 <= w['top'] <= bbox[3]+5)
            _cons, _tier_labels = _consolidate_pref_criteria_table(t, tdata, page)
            _tch_cons, _tch_tier_labels, _two_col_heading_seen_data = _consolidate_two_col_heading_table(
                t, tdata, page, _seen_data_before=_two_col_heading_seen_data)

            # identify tte_score table type
            _tte_cons, _tte_tier_labels = _consolidate_tte_score_table(t, tdata, page, is_continuation = is_cont)

            _n_h, _n_v = _count_internal_grid_lines(page, bbox)
            _has_grid_lines = (_n_h > 0 or _n_v > 0)

            if _has_grid_lines and not _is_spur:
                if _cont_fn_objs:
                    _last_cont = _cont_fn_objs[-1]
                    _cont_bot = _last_cont['top'] + _last_cont.get('size', 8.0) 
                    covered_bboxes.add((pg, bbox[1], _en_top,
                                        pg+1, _cont_bot, _cont_bot))
                else:
                    covered_bboxes.add((pg, bbox[1], _en_top, pg, bbox[3], _en_bot))
                if _title_on_prev_page is not None:
                    covered_bboxes.add((
                        _title_on_prev_page, _title_prev_page_top, _title_prev_page_top,
                        _title_on_prev_page, _title_prev_page_bottom, _title_prev_page_bottom))

            results.append({
                'page': pg,
                'bbox': tuple(round(x, 1) for x in bbox),
                'raw_bbox': tuple(round(x, 1) for x in raw_bbox),
                'tdata': tdata,
                'consolidated_tdata': _cons,
                'has_grid_lines': _has_grid_lines,
                'tier_labels': _tier_labels,
                'two_col_heading_tier_labels': _tch_tier_labels,
                'two_col_heading_consolidated_tdata': _tch_cons,
                'matrix_tier_labels': _matrix_tier_labels if len(tdata) >= 3 else {},
                'matrix_consolidated_tdata': _matrix_cons,
                'tte_tier_labels': _tte_tier_labels,
                'tte_consolidated_tdata': _tte_cons,
                'table_type': ttype,
                'title': title,
                'footnote': _t_footnote,
                'en_top': _en_top if covered else bbox[1],
                'en_bottom': _en_bot if covered else bbox[3],
                'row0': str([str(c)[:20] if c else None for c in row0[:5]]),
                'rows': len(tdata),
                'cols': len(row0),
                'left_words': _left_words,
                'is_continuation': is_cont,
                'covered': covered,
                'gray_bottom': _gray_bottom,
            })

            # Only update prev_bbox for real (non-spurious) tables
            # skip tables nested inside a wrapper on this page -- letting
            # them clobber prev_bbox/prev_row0/prev_ttype would break
            # continuation matching for the NEXT real outer table.
            if not _is_spur and not _is_nested_in_sibling( bbox, page_bboxes ):
                if prev_ttype not in ('two_col_heading', 'two_col_heading_continuation'):
                    _two_col_heading_seen_data = False
                prev_bbox = bbox
                prev_row0 = tdata[0] if tdata else []
                prev_ttype = ttype

    return results, covered_bboxes, _spurious_narrow_rule_bboxes


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
        parts = [p.strip() for p in txt.strip().split('\n') if p.strip()]
        if not parts:
            return ''
        # Join with space — browser handles word wrap naturally
        # Use <br> only if parts look like distinct items (short lines)
        avg_len = sum(len(p) for p in parts) / len(parts)
        if avg_len < 30 and len(parts) > 1:
            # Short lines = likely distinct items, use <br>
            return '<br>'.join(esc(p) for p in parts)
        else:
            # Prose text — join with space for natural word wrap
            return esc(' '.join(parts))

    def _fmt_header_cell(txt: str) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in txt.strip().split('\n') if p.strip()]
        return esc(' '.join(parts))

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

    used_cols = sorted(
        ci for ci in sorted(_header_cols | _data_cols)
        if ((_remapped_header[ci] or '').strip() or
            any(((row[ci] if ci < len(row) else '') or '').strip()
                for row in tdata[1:]))
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
        lines.append(f'     <th>{_fmt_header_cell(cell or "")}</th>')
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
                  'hcpcs', 'cpt', 'icd', 'moa', 'appendix_med', 
                  'generic', 'prod_criteria', 'prod_indications_matrix',
                  'pref_criteria', 'eua_letter', 'cor_loe_recommendation',
                  'medicare_coverage_determination', 'tte_score',
                  'cancer_guidelines', 'fda_device_mfg', 'two_col_heading', ):
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
        _inject_by_page_order(section, tn)


def _inject_by_page_order(section, tn):
    """Insert table at correct position based on page/top ordering."""
    insert_idx = len(section.children)  # default: append at end
    for i, child in enumerate(section.children):
        child_page = getattr(child, 'page', 0)
        child_top  = getattr(child, 'top', 0.0)
        if child_page == 0:
            continue  # skip nodes without page info
        if (child_page > tn.page or
                (child_page == tn.page and child_top > tn.top)):
            insert_idx = i
            break
    section.children.insert(insert_idx, tn)



def _inject_into_subsection_by_page_order(section, tn):
    """
    Find the subsection active when this table appeared (based on page/top),
    and insert the table at the correct position within that subsection.
    If no subsection found, fall back to section-level page order insertion.
    """
    # Find the active subsection for this table's page/top
    active_sub = None
    for child in section.children:
        if isinstance(child, SubsectionNode):
            child_page = getattr(child, 'page', 0)
            child_top  = getattr(child, 'top', 0.0)
            if child_page == 0:
                continue
            if (child_page < tn.page or
                    (child_page == tn.page and child_top <= tn.top)):
                active_sub = child
            elif child_page > tn.page or (
                    child_page == tn.page and child_top > tn.top):
                break

    if active_sub is not None:
        # Insert within the subsection by page/top order
        insert_idx = len(active_sub.children)
        for i, sc in enumerate(active_sub.children):
            sc_page = getattr(sc, 'page', 0)
            sc_top  = getattr(sc, 'top', 0.0)
            if sc_page == 0:
                continue
            if (sc_page > tn.page or
                    (sc_page == tn.page and sc_top > tn.top)):
                insert_idx = i
                break
        active_sub.children.insert(insert_idx, tn)
        return True
    return False


def render_hcpcs_table(tdata: list) -> str:
    """Render a 2-column CPT/HCPCS/ICD-10 coding table.
    Col 0: code (narrow, no wrap)
    Col 1: description (wide, prose wrap)
    """
    from html import escape as esc
    if not tdata:
        return ''

    def _fmt_header(txt: str) -> str:
        if not txt:
            return ''
        return esc(' '.join(p.strip() for p in txt.split('\n') if p.strip()))

    def _fmt_cell(txt: str) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in txt.split('\n') if p.strip()]
        if not parts:
            return ''
        result = esc(parts[0])
        for p in parts[1:]:
            if p.startswith('•') or p.startswith('\u2022') or p.startswith('-'):
                result += '<br>' + esc(p)
            else:
                result += ' ' + esc(p)
        return result

    header_row = tdata[0] if tdata else []
    h0 = _fmt_header((header_row[0] or '') if header_row else '')
    h1 = _fmt_header((header_row[1] or '') if len(header_row) > 1 else '')

    # Build header values tuple for detecting repeated headers in continuations
    _header_vals = tuple(str(c or '').strip() for c in header_row[:2])

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:15%;white-space:nowrap">',
        '    <col style="width:85%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
        f'     <th style="white-space:nowrap">{h0}</th>',
        f'     <th>{h1}</th>',
        '    </tr>',
        '   </thead>',
        '   <tbody>',
    ]

    prev_c0 = None
    prev_c1 = None
    pending_row = None

    for row in tdata[1:]:
        if not row:
            continue
        # Detect repeated header row from continuation page
        _row_vals = tuple(str(c or '').strip() for c in row[:2])
        if _row_vals == _header_vals:
            # Render as header row with gray background
            lines.append('    <tr style="background:#e8e8e8">')
            lines.append(f'     <th style="white-space:nowrap">{_fmt_header(row[0] or "")}</th>')
            lines.append(f'     <th>{_fmt_header(row[1] or "" if len(row) > 1 else "")}</th>')
            lines.append('    </tr>')
            continue

        c0 = (row[0] or '').strip()
        # Find description — first non-empty cell after col0
        c1 = ''
        for ci in range(1, len(row)):
            val = (row[ci] or '').strip()
            if val:
                c1 = val
                break

        if not c0 and c1 and pending_row is not None:
            # Continuation of previous row — append description
            pending_row[1] = pending_row[1] + ' ' + c1 if pending_row[1] else c1
            continue
        
        # Flush pending row
        if pending_row is not None:
            lines.append('    <tr>')
            lines.append(f'     <td style="white-space:nowrap">{_fmt_cell(pending_row[0])}</td>')
            lines.append(f'     <td>{_fmt_cell(pending_row[1])}</td>')
            lines.append('    </tr>')
        
        pending_row = [c0, c1]
    
    # Flush last row
    if pending_row is not None:
        lines.append('    <tr>')
        lines.append(f'     <td style="white-space:nowrap">{_fmt_cell(pending_row[0])}</td>')
        lines.append(f'     <td>{_fmt_cell(pending_row[1])}</td>')
        lines.append('    </tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)



def _classify_table_types(table_info: list, section_boundaries: list,
                           para_cache: dict = None,
                           table_bboxes: set = None,
                           spurious_narrow_rule_bboxes: dict = None, 
                           policy_id: str = '',
                           raw_lines: list = None) -> None:
    """Classify table types using section context."""
    
    def _sec(pg, top):
        return _section_at(section_boundaries, pg, top) if section_boundaries else ''
    
    _cor_loe_active = False
    _cancer_guidelines_active = False
    _indications_active_variant = None            # None / 'covered' / 'non-covered'
    _hct_active = False
    _dql_active = False
    _prod_criteria_active = False
    _fda_dosing_active = False
    _prod_indications_matrix_active = False
    _pref_criteria_active = False
    _tte_score_active = False
    _hcpcs_active = False
    _cpt_active = False
    _icd_active = False
    _two_col_heading_active = False

    _last_outer_type = None
    _last_outer_bbox = None
    _last_outer_page = None
    _last_outer_section = None

    for idx, entry in enumerate(table_info):
        if entry['table_type'] == 'unknown_continuation':
            # If this entry structurally has no real grid lines, it's
            # spurious content (e.g. an unrelated prose paragraph that
            # geometrically happened to look like a continuation of a
            # borderless predecessor), not a genuine table continuation --
            # settle that FIRST, before attempting the lookback-substitution
            # logic below, since that logic would never otherwise be
            # reached for this entry (this branch always `continue`s).
            if not entry.get('has_grid_lines', True):
                entry['table_type'] = 'spurious_no_border'
                continue
 
            # The preceding entry (table_info[idx - 1]) has already been
            # reclassified by this point, since we process in document
            # order and mutate table_info in place. If it turned out to
            # be hcpcs/cpt/icd, fix this entry's substring accordingly.
            if _last_outer_type is not None:
                _prev_base = re.sub(r'(_continuation)+$', '', _last_outer_type)
                if _prev_base in ('hcpcs', 'cpt', 'icd', 'fda_device_mfg',
                                   'medicare_coverage_determination',
                                   'tte_score', 'fda_recommended_dosing',
                                   'Indications_covered', 'Indications_non-covered',
                                   'cancer_guidelines', 'hct_recommendation',
                                   'appendix_med', 'moa', 'criteria', 'criteria_for_use',
                                   'dql', 'proc_code_policy', 'cor_loe_recommendation', 
                                   'prod_indications_matrix', 'eua_letter', 'prod_criteria',
                                   'fda_rx', 'titled_generic', 'nonpref', 'drug_equiv',
                                   'med_moa', 'cond_crit_for_use', 'revision',
                                   'comp_drug_comment_prescr_ingre', 'two_col_heading', 
                                   'pref_criteria'):
                    _blocked = (
                        _prev_base in ('hcpcs', 'cpt', 'icd') and
                        _last_outer_bbox is not None and
                        _has_intervening_content(
                            raw_lines, table_bboxes,
                            _last_outer_page, _last_outer_bbox[3],
                            entry['page'], entry['bbox'][1])
                    )
                    if not _blocked:
                        entry['table_type'] = f'{_prev_base}_continuation'
            continue  # either fixed above, or leave as unknown_continuation

        # Peek at THIS (already-classified or 'unknown') entry to track
        # the dql lineage, before deciding whether to skip it below.
        _existing_ttype = entry.get('table_type')

        _same_page_bboxes = [e['bbox'] for e in table_info if e['page'] == entry['page']]
        _is_nested = _is_nested_in_sibling(entry['bbox'], _same_page_bboxes)
        if not _is_nested:
            if _existing_ttype in ('pref_criteria', 'pref_criteria_continuation'):
                _pref_criteria_active = True
            elif (_existing_ttype not in ('unknown',) and
                    not str(_existing_ttype).startswith('spurious')):
                _pref_criteria_active = False

            if _existing_ttype in ('prod_criteria', 'prod_criteria_continuation'):
                _prod_criteria_active = True
            elif (_existing_ttype not in ('unknown',) and 
                    not str(_existing_ttype).startswith('spurious')):
                _prod_criteria_active = False

            if _existing_ttype in ('dql', 'dql_continuation'):
                _dql_active = True
            elif _existing_ttype not in ('unknown',):
                _dql_active = False   # any OTHER already-classified type breaks the lineage

            if _existing_ttype in ('fda_recommended_dosing', 'fda_recommended_dosing_continuation'):
                _fda_dosing_active = True
            elif _existing_ttype not in ('unknown',):
                _fda_dosing_active = False
            # (same guard should wrap the other _active flags in this
            #  block -- _cor_loe_active, _cancer_guidelines_active,
            #  _indications_active_variant, _hct_active -- once shown)

            # NEW: also track _last_outer_type/_last_outer_bbox here,
            # BEFORE the early-continue below, so already-classified
            # entries (which skip the end-of-loop update) still register
            # themselves as the most recent real outer table.
            if _existing_ttype not in ('unknown',):
                _last_outer_type = _existing_ttype
                _last_outer_bbox = entry['bbox']
                _last_outer_page = entry['page']
                _last_outer_section = _sec(entry['page'], entry['bbox'][1])

        if entry['table_type'] == 'spurious_narrow_rule':
            _tdata_sn = entry.get('tdata') or []
            _sec_sn = _sec(entry['page'], entry['bbox'][1]).lower()
            if 'coding' in _sec_sn:
                for _kind, _cont_ttype in (('hcpcs', 'hcpcs_continuation'),
                                            ('cpt', 'cpt_continuation'),
                                            ('icd', 'icd_continuation')):
                    if _matches_code_lineage(_tdata_sn, _kind):
                        entry['table_type'] = _cont_ttype
                        if table_bboxes is not None and spurious_narrow_rule_bboxes is not None:
                            _key = (entry['page'], entry['bbox'])
                            if _key in spurious_narrow_rule_bboxes:
                                _en_top, _en_bot = spurious_narrow_rule_bboxes[_key]
                                table_bboxes.add((entry['page'], entry['bbox'][1], _en_top,
                                                   entry['page'], entry['bbox'][3], _en_bot))
                        break
            continue  # whether promoted or not, skip the rest of the classification block

        if entry['table_type'] not in ('unknown'):
            continue  # already classified (spurious, continuation, revision, dql, availability)

        if not entry.get('has_grid_lines', True):
            entry['table_type'] = 'spurious_no_border'
            continue
        
        tdata = entry.get('tdata', [])
        row0 = tdata[0] if tdata else []
        pg = entry['page']
        top = entry['bbox'][1]
        sec = _sec(pg, top).lower()

        is_criteria_description = False
        if 'general background' in sec:
            is_criteria_description = _is_criteria_description_table(tdata)

        is_pref_criteria_sticky_continuation = False
        if _pref_criteria_active and _last_outer_bbox is not None:
            _x0_ok = abs(entry['bbox'][0] - _last_outer_bbox[0]) < 5
            _x2_ok = abs(entry['bbox'][2] - _last_outer_bbox[2]) < 30
            _cur_section = _sec(entry['page'], entry['bbox'][1])
            _section_matches = (
                _last_outer_section is not None and
                _cur_section == _last_outer_section
            )

            # Repeated-header continuation: this candidate has its own
            # genuine "Product"/"Exception Criteria" header row (fill-
            # color tiered), which is strong evidence of a legitimate
            # continuation regardless of title -- title-detection can
            # misfire by reaching into the PREVIOUS page's table
            # content when there's no real title, so we don't gate
            # this path on _no_own_title at all.
            _tdata_pc = entry.get('tdata') or []
            _has_own_repeated_header = bool(_tdata_pc) and _looks_like_header_of_kind is not None and False
            _row0_pc = _tdata_pc[0] if _tdata_pc else []
            _row0_nonempty_pc = [(c or '').strip().lower() for c in row0_pc if (c or '').strip()] if False else []

            if _x0_ok and _x2_ok and _section_matches:
                _no_own_title = not (entry.get('title') or '').strip()
                _nonempty_rows = sum(
                    1 for row in _tdata_pc
                    if row and any((c or '').strip() for c in row)
                )
                if _no_own_title and _nonempty_rows >= 1:
                    is_pref_criteria_sticky_continuation = True

        is_prod_criteria_sticky_continuation = False
        if _prod_criteria_active and _last_outer_bbox is not None:
            _x0_ok = abs(entry['bbox'][0] - _last_outer_bbox[0]) < 5
            _x2_ok = abs(entry['bbox'][2] - _last_outer_bbox[2]) < 30
            _no_own_title = not (entry.get('title') or '').strip()
            _cur_section = _sec(entry['page'], entry['bbox'][1])
            _section_matches = (
                _last_outer_section is not None and
                _cur_section == _last_outer_section
            )
            if _x0_ok and _x2_ok and _no_own_title and _section_matches:
                is_prod_criteria_sticky_continuation = _matches_prod_criteria_lineage(tdata)

        is_dql_sticky_continuation = False
        if (_dql_active and 'coverage policy' in sec and 
                _last_outer_type in ('dql', 'dql_continuation')):
            _gap_ok = True
            if _last_outer_bbox is not None and _last_outer_page == entry['page']:
                _gap = entry['bbox'][1] - _last_outer_bbox[3]
                _gap_ok = _gap < 50.0
            if _gap_ok:
                is_dql_sticky_continuation = _matches_dql_lineage(tdata) 

        # detection (reuse _matches_dql_lineage's loose structural check,
        # or write a similarly loose one specifically for this if needed):
        is_fda_dosing_sticky_continuation = False
        if _fda_dosing_active and not _is_nested:
            is_fda_dosing_sticky_continuation = _matches_fda_dosing_lineage(tdata)
            if is_fda_dosing_sticky_continuation:
                ttype = 'fda_recommended_dosing_continuation'

        is_cor_loe_fresh = False
        if 'general background' in sec:
            is_cor_loe_fresh = _is_cor_loe_header(tdata, entry.get('tier_labels'))
 
        is_cor_loe_continuation = False
        if (not is_cor_loe_fresh and _cor_loe_active and
                'general background' in sec):
            is_cor_loe_continuation = _cor_loe_continuation_match(tdata)

        is_indications_fresh = None   # will hold 'covered'/'non-covered' if matched
        if 'coverage policy' in sec:
            is_indications_fresh = _indications_variant_from_header(tdata)
 
        is_indications_continuation = False
        if (not is_indications_fresh and _indications_active_variant and
                'coverage policy' in sec):
            # headerless continuation of the currently active variant --
            # accept any table in Coverage Policy while a lineage is
            # active (mirrors the simplicity of this document's
            # structure; tighten later if this over-matches elsewhere)
            is_indications_continuation = True

        _tier_labels = entry.get('matrix_tier_labels') or {}
        _header2_row_idxs = [i for i, v in _tier_labels.items() if v == 'header2']
        _data_row_idxs = [i for i, v in _tier_labels.items() if v == 'data']

        _matrix_tdata_for_check = entry.get('tdata') or []
        _joined_header2_flat = ' '.join(
            str(c).strip().lower()
            for i in _header2_row_idxs
            if i < len(_matrix_tdata_for_check)
            for c in (_matrix_tdata_for_check[i] or []) if c
        )
        _is_criteria_like_header = (
            'criteria' in _joined_header2_flat or
            'exception' in _joined_header2_flat
        )

        _matrix_consolidated_tdata = entry.get('matrix_consolidated_tdata') or []
        _consolidated_header2_row = (
            _matrix_consolidated_tdata[1] if len(_matrix_consolidated_tdata) > 1 else []
        )
        _header2_col_count = len([
            c for c in _consolidated_header2_row if c and str(c).strip()
        ])
        _has_enough_columns = _header2_col_count > 2
        is_prod_indications_matrix = (
            bool(_header2_row_idxs) and bool(_data_row_idxs) and
            not _is_criteria_like_header and _has_enough_columns
        )

        is_prod_indications_matrix_sticky_continuation = False
        if _prod_indications_matrix_active:
            _tl_values = set(_tier_labels.values())
            is_prod_indications_matrix_sticky_continuation = (
                bool(_tl_values) and _tl_values.issubset({'data', 'subgroup'})
            )

        is_criteria_for_use = False
        _row0_nonempty = [(c or '').strip().lower() for c in (row0 or []) if (c or '').strip()]
        if len(_row0_nonempty) == 2:
            _has_criteria_label = any(c in ('criteria', 'criteria for use') for c in _row0_nonempty)
            _has_score = any(c == 'score' for c in _row0_nonempty)
            _has_nonpref_exception = any('non-preferred' in c or 'exception' in c for c in _row0_nonempty)
            if _has_criteria_label and not _has_score and not _has_nonpref_exception:
                is_criteria_for_use = True

        is_hct_fresh = False
        if 'general background' in sec:
            is_hct_fresh = _is_hct_header(tdata, entry.get('tier_labels'))
 
        is_hct_continuation = False
        if not is_hct_fresh and _hct_active and 'general background' in sec:
            is_hct_continuation = _hct_continuation_match(tdata)

        flat_r0 = ' '.join(str(c) for c in (row0 or []))
        flat_r0r2 = ' '.join(str(c) for row in (tdata[:3] or [])
                             for c in (row or []))
        _flat_r0r2_nospace = ' '.join(
            (c or '').strip() for row in (tdata[:3] or [])
            for c in (row or []) if (c or '').strip())
        
        has_criteria = any(
            re.sub(r'\s+', '', (c or '').strip()).rstrip(':') == 'Criteria'
            for c in row0)
        is_moa = ('Mechanism of Action' in _flat_r0r2_nospace and
                  (row0[0] or '').strip() == '')
        is_eua = bool(len(row0) >= 2 and
                      (row0[0] or '').strip() == 'Date' and
                      'EUA' in (row0[1] or ''))
        is_fda_rx = ('Drug' in flat_r0 and 'Prescribing' in flat_r0)
        is_rev_flex = (row0 and any('Summary of Changes' in (c or '') for c in row0))
        is_nonpref = ('Exception Criteria' in flat_r0 or
                      ('Non-Preferred' in flat_r0 and 'Criteria' in flat_r0) or
                      'Criteria for Use' in flat_r0)
        is_drug_equiv = ('Non-Covered Brand' in flat_r0 or
                         'Non-Covered Product' in flat_r0 or
                         'Bioequivalent' in flat_r0)
        
        # hcpcs/cpt/icd only in Coding Information section
        is_hcpcs = is_cpt = is_icd = False
        from cigna_constants import _CODING_TABLE_SECTION_EXCEPTIONS
        if 'coding' in sec or policy_id.startswith(_CODING_TABLE_SECTION_EXCEPTIONS):
            is_hcpcs = bool(row0 and any(
                'HCPCS' in (c or '') and
                len((c or '').strip()) <= 30 and
                not (c or '').strip().startswith('Note')
                for c in row0[:2]))
            is_cpt = bool(row0 and any(
                'CPT' in (c or '') and 'HCPCS' not in (c or '') and
                len((c or '').strip()) <= 30 and
                not (c or '').strip().startswith('Note')
                for c in row0[:2]))
            is_icd = bool(row0 and 'ICD-10' in (row0[0] or '') and
                         len((row0[0] or '').strip()) <= 30 and
                         not (row0[0] or '').strip().startswith('Note'))
            # Fallback: check left_words when pdfplumber misses left column
            if not is_hcpcs and not is_cpt and not is_icd:
                _left_words = entry.get('left_words', '')
                if 'HCPCS' in _left_words:
                    is_hcpcs = True
                elif 'CPT' in _left_words:
                    is_cpt = True


        # proc_code_policy: Procedure/Indication -> CPT/HCPCS code mapping,
        # only in Coverage Policy section. Distinct from hcpcs/cpt/icd,
        # which live in Coding Information and map code -> description
        # (code is the key there; here the procedure/indication is the key
        # and the code is supporting detail).
        is_proc_code_policy = False
        if 'coverage policy' in sec:
            _proc_label_re = re.compile(r'\b(procedures?|indications?)\b', re.IGNORECASE)
            _code_label_re = re.compile(r'\b(cpt|hcpcs|codes?)\b', re.IGNORECASE)
            _non_empty = [(c or '').strip() for c in row0 if (c or '').strip()]
            if len(_non_empty) >= 2:
                _has_proc_label = any(_proc_label_re.search(c) for c in _non_empty)
                _has_code_label = any(_code_label_re.search(c) for c in _non_empty)
                # exclude ICD-10-PCS "Procedure Codes -> Description" tables,
                # which are code->description, not procedure->code
                _is_code_desc_table = (
                    'ICD-10' in flat_r0 or
                    (len(_non_empty) >= 2 and _non_empty[-1].lower() == 'description')
                )
                is_proc_code_policy = (
                    _has_proc_label and _has_code_label and not _is_code_desc_table
                )


        # fda_device_mfg: FDA device/product approval-history tables
        # (Device or Product | Identifier | Manufacturer | [Decision Date]),
        # heavily merged/blank-celled layout. Scoped to General Background
        # and Coding Information sections only.
        is_fda_device_mfg = False
        if 'general background' in sec or 'coding information' in sec:
            _device_re = re.compile(r'\b(device|product)s?\b', re.IGNORECASE)
            _identifier_re = re.compile(r'\bidentifier\b', re.IGNORECASE)
            _manufacturer_re = re.compile(r'\bmanufacturer\b', re.IGNORECASE)
            _decision_date_re = re.compile(r'\bdecision\s+date\b', re.IGNORECASE)
 
            _non_empty = [(c or '').strip() for c in row0 if (c or '').strip()]
            if 3 <= len(_non_empty) <= 4:
                _has_device = any(_device_re.search(c) for c in _non_empty)
                _has_identifier = any(_identifier_re.search(c) for c in _non_empty)
                _has_manufacturer = any(_manufacturer_re.search(c) for c in _non_empty)
                is_fda_device_mfg = _has_device and _has_identifier and _has_manufacturer


        # medicare_coverage_determination: NCD/LCD reference tables
        # (Contractor | Determination Name/Number | Revision Effective
        # Date), with data rows starting 'NCD' or 'LCD'. Scoped to
        # Medicare Coverage Determinations section only.
        is_mcd = False
        if 'medicare coverage determinations' in sec:
            _contractor_re = re.compile(r'\bcontractor\b', re.IGNORECASE)
            _determination_re = re.compile(r'\bdetermination\s+name', re.IGNORECASE)
            _revision_effective_re = re.compile(r'\brevision\s+effective\b', re.IGNORECASE)
 
            _row1 = tdata[1] if len(tdata) > 1 else []
            _header_cells = [
                (c or '').strip() for c in (list(row0) + list(_row1))
                if (c or '').strip()
            ]
            _has_contractor = any(_contractor_re.search(c) for c in _header_cells)
            _has_determination = any(_determination_re.search(c) for c in _header_cells)
            _has_revision_effective = any(_revision_effective_re.search(c) for c in _header_cells)
 
            if _has_contractor and _has_determination and _has_revision_effective:
                is_mcd = any(
                    row and (row[0] or '').strip() in ('NCD', 'LCD')
                    for row in tdata[1:]
                )


        # TTE score table detection
        is_tte_score_table = False
        if 'general background' in sec:
            is_tte_score_table = _is_tte_score_table_content(tdata)


        # Cancer guidelines table detection
        is_cancer_guidelines, _cancer_aligned = _classify_cancer_guidelines(
            tdata, entry.get('tier_labels'), sec)
        if is_cancer_guidelines:
            entry['consolidated_tdata'] = _cancer_aligned

        # Cancer guidelines continuation table detection
        is_cancer_guidelines_continuation = False
        if (not is_cancer_guidelines and _cancer_guidelines_active and
                'general background' in sec):
            is_cancer_guidelines_continuation = _cancer_guidelines_continuation_match(tdata)

 
        # appendix tables after Revision Details or References section
        is_appendix_med = False
        if 'appendix' in sec.lower():
            _flat_row0 = ' '.join(str(c or '') for c in row0)
            if 'Medication' in _flat_row0 and 'Mode of Administration' in _flat_row0:
                # Remap 6-col to 2-col
                _h_med  = next((i for i, c in enumerate(row0)
                                if 'Medication' in (c or '')), None)
                _h_mode = next((i for i, c in enumerate(row0)
                                if 'Mode of Administration' in (c or '')), None)
                if _h_med is not None and _h_mode is not None:
                    _data_row = tdata[1] if len(tdata) > 1 else []
                    _d_med  = next((i for i, c in enumerate(_data_row)
                                   if (c or '').strip() and i < _h_mode), _h_med)
                    _d_mode = next((i for i, c in enumerate(_data_row)
                                   if (c or '').strip() and i > _d_med), _h_mode)
                    new_tdata = [['Medication', 'Mode of Administration']]
                    for row in tdata[1:]:
                        med  = (row[_d_med]  or '').strip() if _d_med  < len(row) else ''
                        mode = (row[_d_mode] or '').strip() if _d_mode < len(row) else ''
                        new_tdata.append([med, mode])
                    entry['tdata'] = new_tdata
                is_appendix_med = True
        
        title = entry.get('title', '')
        _title_norm = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', title)
        _title_lc = _title_norm.lower()
        is_titled = bool(re.match(r'(Appendix\s+)?Table\s+\d+[.]', title))
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
            re.match(r'Table\s+\d+[:\s]', _title_norm) is not None)
        
        # Suppress title for hcpcs/cpt/icd tables
        if is_hcpcs or is_cpt or is_icd:
            entry['title'] = ''
        
        ttype = 'unknown'
        if has_criteria:
            ttype = 'criteria'
        elif is_hcpcs:
            _is_cont = (_hcpcs_active and entry.get('is_continuation') and
                        _last_outer_bbox is not None and
                        not _has_intervening_content(
                            para_cache, table_bboxes,
                            _last_outer_page, _last_outer_bbox[3],
                            entry['page'], entry['bbox'][1]))
            ttype = 'hcpcs_continuation' if _is_cont else 'hcpcs'
        elif is_cpt:
            _is_cont = (_cpt_active and entry.get('is_continuation') and
                        _last_outer_bbox is not None and
                        not _has_intervening_content(
                            para_cache, table_bboxes,
                            _last_outer_page, _last_outer_bbox[3],
                            entry['page'], entry['bbox'][1]))
            ttype = 'cpt_continuation' if _is_cont else 'cpt'
        elif is_icd:
            _is_cont = (_icd_active and entry.get('is_continuation') and
                        not _has_intervening_content(
                            para_cache, table_bboxes,
                            _last_outer_page, _last_outer_bbox[3],
                            entry['page'], entry['bbox'][1]))
            ttype = 'icd_continuation' if _is_cont else 'icd'
        elif is_prod_criteria_sticky_continuation:
            ttype = 'prod_criteria_continuation'
        elif is_pref_criteria_sticky_continuation:
            ttype = 'pref_criteria_continuation'
        elif is_dql_sticky_continuation:
            ttype = 'dql_continuation'
        elif is_fda_dosing_sticky_continuation:
            ttype = 'fda_recommended_dosing_continuation'
        elif is_moa:
            ttype = 'moa'
        elif is_eua:
            ttype = 'eua_letter'
        elif is_fda_rx:
            ttype = 'fda_rx'
        elif is_rev_flex:
            ttype = 'revision'
        elif is_appendix_med:
            ttype = 'appendix_med'
        elif is_proc_code_policy:
            ttype = 'proc_code_policy'
        elif is_fda_device_mfg:
            ttype = 'fda_device_mfg'
        elif is_mcd:
            ttype = 'medicare_coverage_determination'
        elif is_tte_score_table:
            _is_fresh_looking = _tte_score_looks_like_fresh_header(tdata)
            ttype = 'tte_score' if (not _tte_score_active or _is_fresh_looking) else 'tte_score_continuation'
        elif is_cor_loe_fresh:
            ttype = 'cor_loe_recommendation'
        elif is_cor_loe_continuation:
            ttype = 'cor_loe_recommendation_continuation'
        elif is_cancer_guidelines:
            ttype = 'cancer_guidelines'
        elif is_cancer_guidelines_continuation:
            ttype = 'cancer_guidelines_continuation'
        elif is_criteria_description:
            ttype = 'criteria_description'
        elif is_criteria_for_use:
            ttype = 'criteria_for_use'
        elif is_indications_fresh == 'covered':
            ttype = 'Indications_covered'
        elif is_indications_fresh == 'non-covered':
            ttype = 'Indications_non-covered'
        elif is_indications_continuation:
            ttype = f'Indications_{_indications_active_variant}_continuation'
        elif is_hct_fresh:
            ttype = 'hct_recommendation'
        elif is_hct_continuation:
            ttype = 'hct_recommendation_continuation'
        elif is_prod_indications_matrix:
            ttype = 'prod_indications_matrix'
            tdata = entry.get('matrix_consolidated_tdata', tdata)
        elif is_prod_indications_matrix_sticky_continuation:
            ttype = 'prod_indications_matrix_continuation'
        elif is_titled or is_titled_known:
            ttype = 'titled_generic'
        elif is_nonpref:
            ttype = 'nonpref'
        elif is_drug_equiv:
            ttype = 'drug_equiv'
        elif ('Medication' in flat_r0 and 'Mode of Administration' in flat_r0r2):
            ttype = 'med_moa'
        elif ('Condition' in flat_r0 and 'Criteria for Use' in flat_r0r2):
            ttype = 'cond_crit_for_use'
        elif any(kw in flat_r0 for kw in (
                 'Compound Name', 'Drug Name', 'Comments',
                 'Prescribing Information', 'Ingredient')):
            ttype = 'comp_drug_comment_prescr_ingre'

        _tier_labels_for_check = entry.get('tier_labels') or {}
        is_two_col_heading_sticky_continuation = False
        if _two_col_heading_active:
            _tl_values = set(_tier_labels_for_check.values())
            is_two_col_heading_sticky_continuation = (
                bool(_tl_values) and _tl_values.issubset({'data', 'subheader', 'header'})
            )

        if ttype in (None, 'unknown',) and is_two_col_heading_sticky_continuation:
            ttype = 'two_col_heading_continuation'
        elif ttype in (None, 'unknown',) and (len(row0) >= 2):
            _data_row_idxs = [i for i, lbl in _tier_labels_for_check.items() if lbl == 'data']
            _max_nonempty_in_data_rows = max(
                (sum(1 for c in (tdata[i] or []) if c and str(c).strip())
                 for i in _data_row_idxs if i < len(tdata)),
                default=0
            )
            _is_two_col_heading = (
                len(row0) >= 2 and
                _max_nonempty_in_data_rows <= 2
            )
            if _is_two_col_heading:
                ttype = 'two_col_heading'
            else:
                ttype = 'unknown'
        
        if ttype in ('generic', 'unknown', 'generic_continuation', 'unknown_continuation') \
                and not entry.get('has_grid_lines', True):
            ttype = 'spurious_no_border'

        if ttype in ('two_col_heading', 'two_col_heading_continuation'):
            _two_col_heading_active = True
        else:
            _two_col_heading_active = False

        is_two_col_heading_sticky_continuation = False
        if _two_col_heading_active:
            _tl_values = set(_tier_labels_for_check.values())
            is_two_col_heading_sticky_continuation = (
                bool(_tl_values) and _tl_values.issubset({'data', 'subheader', 'header'})
            )

        if ttype in ('cor_loe_recommendation', 'cor_loe_recommendation_continuation'):
            _cor_loe_active = True
        else:
            _cor_loe_active = False   # conservative: break the lineage on ANYTHING else

        if ttype in ('cancer_guidelines', 'cancer_guidelines_continuation'):
            _cancer_guidelines_active = True

        if ttype in ('hcpcs_continuation', 'cpt_continuation', 'icd_continuation') and \
                _existing_ttype == 'spurious_narrow_rule' and \
                table_bboxes is not None and spurious_narrow_rule_bboxes is not None:
            _key = (entry['page'], entry['bbox'])
            if _key in spurious_narrow_rule_bboxes:
                _en_top, _en_bot = spurious_narrow_rule_bboxes[_key]
                table_bboxes.add((entry['page'], entry['bbox'][1], _en_top,
                                   entry['page'], entry['bbox'][3], _en_bot))

        if is_indications_fresh:
            _indications_active_variant = is_indications_fresh
        elif ttype not in ('Indications_covered', 'Indications_non-covered') and \
                not (isinstance(ttype, str) and ttype.startswith('Indications_') and \
                ttype.endswith('_continuation')):
            _indications_active_variant = None   # break lineage on anything unrelated

        if ttype in ('hct_recommendation', 'hct_recommendation_continuation'):
            _hct_active = True

        if ttype in ('dql', 'dql_continuation'):
            _dql_active = True
        else:
            _dql_active = False

        # sticky update after ttype finalized:
        if ttype in ('fda_recommended_dosing', 'fda_recommended_dosing_continuation'):
            _fda_dosing_active = True
        else:
            _fda_dosing_active = False

        entry['table_type'] = ttype
        if not _is_nested:
            _last_outer_type = entry['table_type']
            _last_outer_bbox = entry['bbox']
            _last_outer_page = entry['page']
            _last_outer_section = _sec(entry['page'], entry['bbox'][1])
            if ttype in ('prod_indications_matrix', 'prod_indications_matrix_continuation'):
                _prod_indications_matrix_active = True
            elif not str(ttype).startswith('spurious'):
                _prod_indications_matrix_active = False
            if ttype in ('two_col_heading', 'two_col_heading_continuation'):
                _two_col_heading_active = True
            elif not str(ttype).startswith('spurious'):
                _two_col_heading_active = False
            if ttype in ('tte_score', 'tte_score_continuation'):
                _tte_score_active = True
            elif not str(ttype).startswith('spurious'):
                _tte_score_active = False
            if ttype in ('hcpcs', 'hcpcs_continuation'):
                _hcpcs_active = True
            elif not str(ttype).startswith('spurious'):
                _hcpcs_active = False
            if ttype in ('cpt', 'cpt_continuation'):
                _cpt_active = True
            elif not str(ttype).startswith('spurious'):
                _cpt_active = False
            if ttype in ('icd', 'icd_continuation'):
                _icd_active = True
            elif not str(ttype).startswith('spurious'):
                _icd_active = False



def _recover_borderless_first_row(t, tdata, page, bbox, min_extension=5.0):
    """
    Some tables have a first data row with no top ruling line, so
    find_tables() reports bbox[1] as the bottom of the missing row
    rather than its true top. Use _true_table_top to find the real
    top from the table's own vertical border rects (unambiguous --
    no guessing from text gaps), then extract that recovered band's
    text per column the normal way (crop + extract_text), matching
    how every other row's text is obtained.
    """
    if not tdata or not tdata[0]:
        return tdata, bbox

    real_top = _true_table_top(bbox, page.rects)
    extension = bbox[1] - real_top
    if extension < min_extension:
        return tdata, bbox   # no open-top border found -- nothing to recover

    n_cols = len(tdata[0])
    try:
        col_x_bounds = [bbox[0]] + [cell[2] for cell in t.rows[0].cells]
        if len(col_x_bounds) != n_cols + 1:
            raise ValueError
    except Exception:
        col_x_bounds = [bbox[0] + (bbox[2] - bbox[0]) * i / n_cols
                        for i in range(n_cols + 1)]

    new_row = []
    for ci in range(n_cols):
        col_bbox = (col_x_bounds[ci], real_top, col_x_bounds[ci + 1], bbox[1])
        text = page.crop(col_bbox).extract_text() or ''
        new_row.append(' '.join(text.split()))

    if not any(c.strip() for c in new_row):
        return tdata, bbox

    new_bbox = (bbox[0], real_top, bbox[2], bbox[3])
    return [new_row] + list(tdata), new_bbox





def _row_fill(row_obj, page):
    if row_obj is None:
        return None
    rtop, rbot = row_obj.bbox[1], row_obj.bbox[3]
    best = None
    for r in page.rects:
        if not r.get('fill'):
            continue
        if (r['x1'] - r['x0']) < 10:
            continue
        col = r.get('non_stroking_color', 0)
        if isinstance(col, (list, tuple)):
            col = sum(col) / len(col) if col else 0
        if r['top'] <= rtop + 1 and r['bottom'] >= rbot - 1:
            if best is None or float(col) > 0:
                best = round(float(col), 3)
    return best



def extract_medicare_coverage_determinations(rows_with_labels: list) -> list[dict]:
    """
    Extract (determination_type, contractor, determination_name,
    revision_effective_date) tuples from medicare_coverage_determination
    rows_with_labels (label, row) pairs, header rows already excluded
    by caller. Raw tdata shape observed: index 0 = NCD/LCD type,
    index 1 = contractor, index 2 = determination name/number,
    index 3 = revision effective date (phantom empty columns at other
    indices are ignored).
    """
    results = []
    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if not row:
            continue
        determination_type = (str(row[0]).strip() if len(row) > 0 and row[0] else '')
        contractor = (str(row[1]).strip() if len(row) > 1 and row[1] else '')
        determination_name = (str(row[2]).strip() if len(row) > 2 and row[2] else '')
        revision_date = (str(row[3]).strip() if len(row) > 3 and row[3] else '')
        if not determination_type and not determination_name:
            continue
        results.append({
            'determination_type': determination_type or None,
            'contractor': contractor or None,
            'determination_name': determination_name or None,
            'revision_effective_date': revision_date or None,
        })
    return results



def _consolidate_pref_criteria_table(t, tdata, page):
    if not tdata:
        return tdata, {}

    try:
        table_rows = t.rows
    except Exception:
        table_rows = None

    fills = []
    if table_rows and len(table_rows) == len(tdata):
        for _i, row_obj in enumerate(table_rows):
            _f = _row_fill(row_obj, page)
            fills.append(_f)
    else:
        fills = [None] * len(tdata)

    # Group consecutive rows with same fill into tiers
    tiers = []  # list of (fill_value, [row_indices])
    for i, f in enumerate(fills):
        if tiers and tiers[-1][0] == f:
            tiers[-1][1].append(i)
        else:
            tiers.append((f, [i]))

    # Within each tier, find columns where at least one row has non-empty content
    consolidated = []
    tier_labels = {}  # row_index -> 'header'/'subheader'/'data'
    for _fill, row_idxs in tiers:
        tier_rows = [tdata[i] for i in row_idxs]
        n_cols = max((len(r) for r in tier_rows if r), default=0)

        surviving_cols = [
            c for c in range(n_cols)
            if any((r[c] if r and c < len(r) else None) and
                   str(r[c]).strip() for r in tier_rows)
        ]

        # Label the tier
        if _fill is not None and 0.7 <= _fill < 0.92:
            label = 'header'
        elif _fill is not None and 0.92 <= _fill < 1.0:
            label = 'subheader'
        else:
            label = 'data'

        for idx in row_idxs:
            tier_labels[idx] = label

        for r in tier_rows:
            new_row = [r[c] if r and c < len(r) else None for c in surviving_cols]
            consolidated.append(new_row)

    return consolidated, tier_labels



def _rescue_pref_criteria_continuations(table_info, section_boundaries):
    def _sec(pg, top):
        if not section_boundaries:
            return ''
        active = ''
        for b in section_boundaries:
            if b['page'] < pg or (b['page'] == pg and b['top'] <= top):
                active = b['segment']
            else:
                break
        return active

    prev_pref = None
    for entry in table_info:
        ttype = entry.get('table_type', '')
        if ttype in ('pref_criteria', 'pref_criteria_continuation'):
            prev_pref = entry
            continue

        # There is no 'spurious_continuous' table type
        _is_spurious_like = (ttype == 'spurious' or ttype.startswith('spurious_'))
        if (not (_is_spurious_like or ttype in ('spurious', 'unknown', 'unknown_continuation', 
                'generic') ) or prev_pref is None):
            continue

        sec = _sec(entry['page'], entry['bbox'][1])
        if 'coverage' not in sec.lower():
            continue

        _cons = entry.get('consolidated_tdata') or []
        _labels = entry.get('tier_labels') or {}
        if not _cons:
            continue

        # Check consolidated result: must have at least one data-tier row,
        # and consolidated columns must be ≤ 2
        has_data = any(lbl == 'data' for lbl in _labels.values())
        max_cols = max((len(r) for r in _cons if r), default=0)
        if has_data and max_cols <= 2:
            entry['table_type'] = 'pref_criteria_continuation'
            entry['tdata'] = _cons
            entry['covered'] = True
            prev_pref = entry


def consolidate_prod_criteria_rows(rows_with_labels: list) -> list[tuple[str, str]]:
    """
    Shared row-processing logic for prod_criteria tables, used by BOTH
    the HTML renderer (render_prod_criteria_table_from_labeled) and the
    DB loader (load_policy_extract.py), so a fix to one always applies
    to the other.

    Given (label, row) pairs (row 0 is the header, rest are data/
    subheader rows from a fully-merged, continuation-included table):
      - Skips 'subheader' rows entirely (stray fragment rows, e.g. a
        wrapped generic-name continuation line).
      - For each remaining row, picks the LONGEST nonempty cell from
        index 1 onward as the criteria text (raw phantom/duplicate
        columns can put a short duplicate of the product name at an
        early index and the real criteria text at a later one -- the
        longest cell is reliably the real content).
      - Rows with an empty product-name column (col 0) and nonempty
        criteria text are continuation rows: their text is appended
        (newline-joined) to the previous row's criteria text rather
        than starting a new row.

    Returns a list of (product_name, criteria_text) tuples, one per
    real product entry, in order.
    """
    results: list[list[str]] = []  # list of [product_name, criteria_text], mutated in place

    for lbl, row in rows_with_labels:
        if lbl in ('header', 'subheader' if False else 'header'):
            pass
        if lbl == 'header':
            continue  # skip ALL header-tagged rows, not just the first
        if lbl == 'subheader':
            continue
        if not row:
            continue

        c0 = (row[0] or '').strip()
        _candidates = [(row[ci] or '').strip() for ci in range(1, len(row))
                       if row[ci] and str(row[ci]).strip()]
        c1 = max(_candidates, key=len) if _candidates else ''

        if not c0 and c1 and results:
            # Continuation row: append to the previous entry's criteria text.
            prev = results[-1]
            prev[1] = (prev[1] + '\n' + c1) if prev[1] else c1
            continue

        if not c0 and not c1:
            continue

        results.append([c0, c1])

    return [(p, c) for p, c in results]


def render_prod_criteria_table_from_labeled(rows_with_labels: list) -> str:
    """Render a 2-column Product/Criteria table from (label, row) pairs.
    Col 0: product name (wraps, moderate width)
    Col 1: narrative medical-necessity criteria (wide, preserves
           internal list structure -- a./b./i./ii. sub-lists, etc.)
    Continuation pages arrive as single rows with an empty col 0 and
    a large text blob in col 1; these are appended to the previous
    product's criteria cell rather than starting a new row.
    Rows labeled 'subheader' are skipped entirely (stray fragment
    rows, e.g. a wrapped generic-name continuation line, that don't
    carry genuine tabular content).
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_header(txt: str) -> str:
        if not txt:
            return ''
        return esc(' '.join(p.strip() for p in txt.split('\n') if p.strip()))

    def _fmt_product_cell(txt: str) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in txt.split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    def _fmt_criteria_cell(txt: str) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in txt.split('\n') if p.strip()]
        if not parts:
            return ''

        _LIST_ITEM_RE = re.compile(
            r'^(?:[a-zA-Z]\.|[ivxIVX]+\.|\d+\.|\(\d+\)|Note:)\s'
        )

        out = [esc(parts[0])]
        for p in parts[1:]:
            if _LIST_ITEM_RE.match(p):
                out.append('<br>' + esc(p))
            else:
                out.append(' ' + esc(p))
        return ''.join(out)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']
    _nonempty_header_cells = []
    for c in range(max((len(r) for r in header_rows), default=0)):
        parts = [str(r[c]).strip() for r in header_rows
                 if r and c < len(r) and r[c] and str(r[c]).strip()]
        if parts:
            _nonempty_header_cells.append(' '.join(parts))
    h0 = _fmt_header(_nonempty_header_cells[0] if len(_nonempty_header_cells) > 0 else '')
    h1 = _fmt_header(_nonempty_header_cells[1] if len(_nonempty_header_cells) > 1 else '')

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:22%">',
        '    <col style="width:78%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
        f'     <th>{h0}</th>',
        f'     <th>{h1}</th>',
        '    </tr>',
        '   </thead>',
        '   <tbody>',
    ]

    for product_name, criteria_text in consolidate_prod_criteria_rows(rows_with_labels):
        lines.append('    <tr>')
        lines.append(f'     <td>{_fmt_product_cell(product_name)}</td>')
        lines.append(f'     <td>{_fmt_criteria_cell(criteria_text)}</td>')
        lines.append('    </tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def render_prod_indications_matrix_table_from_labeled(rows_with_labels: list) -> str:
    from html import escape as esc
    if not rows_with_labels:
        return ''

    header1_row = next((r for lbl, r in rows_with_labels if lbl == 'header1'), None)
    header2_rows = [r for lbl, r in rows_with_labels if lbl == 'header2']
    body_rows = [(lbl, r) for lbl, r in rows_with_labels if lbl not in ('header1', 'header2')]

    n_cols = max((len(r) for _, r in rows_with_labels if r), default=0)
    n_cols = max(n_cols, max((len(r) for r in header2_rows if r), default=0))
    if header1_row:
        n_cols = max(n_cols, len(header1_row))

    joined_header2 = []
    for c in range(n_cols):
        parts = [str(r[c]).strip() for r in header2_rows
                 if c < len(r) and r[c] and str(r[c]).strip()]
        joined_header2.append(esc(' '.join(p.replace('\n', ' ') for p in parts)))

    col_pct = 100 / n_cols if n_cols else 100
    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
    ]
    for _ in range(n_cols):
        lines.append(f'    <col style="width:{col_pct:.1f}%">')
    lines.append('   </colgroup>')
    lines.append('   <thead>')

    if header1_row:
        lines.append('    <tr style="background:#e8e8e8">')
        c = 0
        while c < n_cols:
            _val = header1_row[c] if c < len(header1_row) else None
            _span = 1
            _next = c + 1
            while _next < n_cols and not (header1_row[_next] if _next < len(header1_row) else None):
                _span += 1
                _next += 1
            if _val and str(_val).strip():
                lines.append(f'     <th colspan="{_span}">{esc(str(_val).strip())}</th>')
            else:
                lines.append(f'     <th colspan="{_span}"></th>')
            c = _next
        lines.append('    </tr>')

    lines.append('    <tr style="background:#e8e8e8">')
    for h in joined_header2:
        lines.append(f'     <th>{h}</th>')
    lines.append('    </tr>')
    lines.append('   </thead>')
    lines.append('   <tbody>')

    for lbl, r in body_rows:
        padded = list(r or []) + [None] * (n_cols - len(r or []))
        if lbl == 'subgroup':
            text = next((c for c in padded if c and str(c).strip()), '')
            lines.append(
                f'    <tr><td colspan="{n_cols}" '
                f'style="background:#ffffff;font-weight:bold">'
                f'{esc(str(text).strip())}</td></tr>')
        else:
            cells = [_fmt_matrix_cell(c) for c in padded[:n_cols]]
            lines.append('    <tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def _fmt_matrix_cell(txt) -> str:
    from html import escape as esc
    if txt is None:
        return ''
    txt = str(txt)
    parts = [p.strip() for p in txt.split('\n') if p.strip()]
    if not parts:
        return ''
    out = [esc(parts[0])]
    for p in parts[1:]:
        if p.startswith('•') or p.startswith('\u2022'):
            out.append('<br>' + esc(p))
        else:
            out.append(' ' + esc(p))
    return ''.join(out)


def consolidate_pref_criteria_rows(rows_with_labels: list) -> list[tuple[str, str, bool]]:
    """
    Shared row-processing logic for pref_criteria tables, used by BOTH
    the HTML renderer (render_pref_criteria_table_from_labeled) and the
    DB loader (load_policy_extract.py), so a fix to one always applies
    to the other.

    Given (label, row) pairs (header rows already excluded by the
    caller), returns a list of (product_name, criteria_text,
    is_subheader) tuples:
      - A 'subheader' row becomes (heading_text, '', True) -- a
        spanning divider, e.g. "Tumor Necrosis Factor Inhibitors".
      - Any other row becomes (product_name, criteria_text, False),
        reading product_name from column 0 and criteria_text from
        column 1 directly (pref_criteria's raw tdata is reliably
        2-column, unlike prod_criteria's frequent phantom columns).
    """
    results = []
    for lbl, r in rows_with_labels:
        if lbl == 'header':
            continue
        if lbl == 'subheader':
            text = next((c for c in (r or []) if c and str(c).strip()), '')
            results.append((str(text).strip(), '', True))
        else:
            c0 = r[0] if r and len(r) > 0 else None
            c1 = r[1] if r and len(r) > 1 else None
            results.append((
                (str(c0).strip() if c0 else ''),
                (str(c1).strip() if c1 else ''),
                False,
            ))
    return results


def render_pref_criteria_table_from_labeled(rows_with_labels: list) -> str:
    """
    Render a pref_criteria table: 2-column header (possibly wrapped
    across 2 physical rows, e.g. 'Non-Preferred' + 'Product'),
    single-cell 'subheader' divider rows, and data rows (product name
    + long narrative exception-criteria text).
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_criteria_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        if not parts:
            return ''
        _LIST_ITEM_RE = re.compile(
            r'^(?:[a-zA-Z]\.|[ivxIVX]+\.|\d+\.|\(\d+\)|[A-Z]\)|Note:)\s'
        )
        out = [esc(parts[0])]
        for p in parts[1:]:
            if _LIST_ITEM_RE.match(p):
                out.append('<br>' + esc(p))
            else:
                out.append(' ' + esc(p))
        return ''.join(out)

    def _fmt_product_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']

    n_cols = 2  # Non-Preferred Product / Exception Criteria
    joined_header = []
    for c in range(n_cols):
        parts = [str(r[c]).strip() for r in header_rows
                 if r and c < len(r) and r[c] and str(r[c]).strip()]
        joined_header.append(esc(' '.join(parts)))

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:20%">',
        '    <col style="width:80%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
    ]
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    for product_name, criteria_text, is_subheader in consolidate_pref_criteria_rows(rows_with_labels):
        if is_subheader:
            lines.append(
                f'    <tr><td colspan="{n_cols}" '
                f'style="background:#ffffff;font-weight:bold">'
                f'{esc(product_name)}</td></tr>')
        else:
            lines.append(
                f'    <tr><td style="vertical-align:top">{_fmt_product_cell(product_name)}</td>'
                f'<td style="vertical-align:top">{_fmt_criteria_cell(criteria_text)}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def render_eua_letter_table(tdata: list) -> str:
    """Render an EUA Letter table: Date / EUA Letter narrative text."""
    from html import escape as esc
    if not tdata:
        return ''

    def _fmt_header(txt: str) -> str:
        if not txt:
            return ''
        return esc(' '.join(p.strip() for p in txt.split('\n') if p.strip()))

    def _fmt_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        if not parts:
            return ''
        _LIST_ITEM_RE = re.compile(r'^(?:[a-zA-Z]\.|[ivxIVX]+\.|\d+\.|\(\d+\)|•|\u2022)\s')
        out = [esc(parts[0])]
        for p in parts[1:]:
            if _LIST_ITEM_RE.match(p):
                out.append('<br>' + esc(p))
            else:
                out.append(' ' + esc(p))
        return ''.join(out)

    header_row = tdata[0] if tdata else []
    h0 = _fmt_header((header_row[0] or '') if header_row else '')
    h1 = _fmt_header((header_row[1] or '') if len(header_row) > 1 else '')

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:12%;white-space:nowrap">',
        '    <col style="width:88%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
        f'     <th style="white-space:nowrap">{h0}</th>',
        f'     <th>{h1}</th>',
        '    </tr>',
        '   </thead>',
        '   <tbody>',
    ]

    for row in tdata[1:]:
        if not row:
            continue
        c0 = row[0] if len(row) > 0 else None
        c1 = row[1] if len(row) > 1 else None
        lines.append(
            f'    <tr><td style="white-space:nowrap;vertical-align:top">{esc(str(c0 or ""))}</td>'
            f'<td style="vertical-align:top">{_fmt_cell(c1)}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def render_cor_loe_table(tdata: list, tier_labels: dict,
                          carry_in_subheading: str = None) -> tuple[str, str | None]:
    from html import escape as esc
    if not tdata:
        return '', None

    def _fmt_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        if not parts:
            return ''
        _LIST_ITEM_RE = re.compile(r'^(?:[a-zA-Z]\.|[ivxIVX]+\.|\d+\.|\(\d+\)|•|\u2022)\s')
        out = [esc(parts[0])]
        for p in parts[1:]:
            if _LIST_ITEM_RE.match(p):
                out.append('<br>' + esc(p))
            else:
                out.append(' ' + esc(p))
        return ''.join(out)

    labels_in_order = ([tier_labels[i] for i in sorted(tier_labels)]
                        if tier_labels and len(tier_labels) == len(tdata)
                        else ['data'] * len(tdata))

    rows = [(lbl, row) for lbl, row in zip(labels_in_order, tdata)
            if row and any(c and str(c).strip() for c in row)]

    has_real_header_tag = any(lbl == 'header' for lbl, _ in rows)

    header_rows = []
    header_cols_used = set()
    body = []
    in_header = True
    for lbl, row in rows:
        nonempty_cols = {i for i, c in enumerate(row) if c and str(c).strip()}
        if in_header:
            if has_real_header_tag:
                if lbl == 'header':
                    header_rows.append(row)
                    continue
                else:
                    in_header = False
            else:
                if lbl == 'subheader':
                    if header_rows and not (nonempty_cols & header_cols_used):
                        in_header = False
                    else:
                        header_rows.append(row)
                        header_cols_used |= nonempty_cols
                        continue
                else:
                    in_header = False

        nonempty = [c for c in row if c and str(c).strip()]
        if len(nonempty) <= 1:
            body.append(('divider', row))
        else:
            body.append(('data', row))

    n_cols = max((len(r) for r in tdata if r), default=0)
    joined_header = []
    for c in range(n_cols):
        parts = [str(r[c]).strip().replace('\n', ' ') for r in header_rows
                 if r and c < len(r) and r[c] and str(r[c]).strip()]
        if parts:
            joined_header.append(esc(' '.join(parts)))
    if not joined_header:
        joined_header = ['']

    n_display_cols = len(joined_header)

    # Inject carried-forward subheading if this table's body starts
    # straight into data rows with no divider of its own.
    if carry_in_subheading and body and body[0][0] != 'divider':
        body.insert(0, ('divider', [carry_in_subheading]))

    col_pct = 100 / n_display_cols if n_display_cols else 100
    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
    ]
    for _ in range(n_display_cols):
        lines.append(f'    <col style="width:{col_pct:.1f}%">')
    lines.append('   </colgroup>')
    lines.append('   <thead>')
    lines.append('    <tr style="background:#e8e8e8">')
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    last_subheading = carry_in_subheading
    for kind, row in body:
        if kind == 'divider':
            text = next((c for c in row if c and str(c).strip()), '')
            text = str(text).strip()
            last_subheading = text
            lines.append(
                f'    <tr><td colspan="{n_display_cols}" '
                f'style="background:#ffffff;font-weight:bold">'
                f'{esc(text)}</td></tr>')
        else:
            cells_text = [c for c in row if c and str(c).strip()]
            padded = cells_text + [None] * (n_display_cols - len(cells_text))
            cells = [f'<td style="vertical-align:top">{_fmt_cell(c)}</td>'
                     for c in padded[:n_display_cols]]
            lines.append('    <tr>' + ''.join(cells) + '</tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines), last_subheading


def render_mcd_table(tdata: list, tier_labels: dict) -> str:
    """Render a medicare_coverage_determination table. First data column
    is Type (NCD/LCD) with no header label of its own; remaining columns
    are Contractor / Determination Name/Number / Revision Effective Date.
    Header and data rows can have slightly different raw column indices
    (pdfplumber quirk), so header and data columns are pruned and
    order-matched independently rather than by shared raw index."""
    from html import escape as esc
    if not tdata:
        return ''

    def _fmt_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    labels_in_order = ([tier_labels[i] for i in sorted(tier_labels)]
                        if tier_labels and len(tier_labels) == len(tdata)
                        else ['data'] * len(tdata))

    rows = [(lbl, row) for lbl, row in zip(labels_in_order, tdata)
            if row and any(c and str(c).strip() for c in row)]

    header_rows = [row for lbl, row in rows if lbl == 'header']
    data_rows = [row for lbl, row in rows if lbl != 'header']

    n_cols = max((len(r) for r in tdata if r), default=0)

    header_surviving_cols = [
        c for c in range(n_cols)
        if any((r[c] if r and c < len(r) else None) and str(r[c]).strip()
               for r in header_rows)
    ]
    data_surviving_cols = [
        c for c in range(n_cols)
        if any((r[c] if r and c < len(r) else None) and str(r[c]).strip()
               for r in data_rows)
    ]

    joined_header = []
    for c in header_surviving_cols:
        parts = [str(r[c]).strip().replace('\n', ' ') for r in header_rows
                 if r and c < len(r) and r[c] and str(r[c]).strip()]
        joined_header.append(esc(' '.join(parts)) if parts else '')

    # Type (NCD/LCD) column has no header label -- always prepend a
    # blank leading header slot to align with data's leading column.
    joined_header = [''] + joined_header
    n_display_cols = len(joined_header)

    col_pct = 100 / n_display_cols if n_display_cols else 100
    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
    ]
    for _ in range(n_display_cols):
        lines.append(f'    <col style="width:{col_pct:.1f}%">')
    lines.append('   </colgroup>')
    lines.append('   <thead>')
    lines.append('    <tr style="background:#e8e8e8">')
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    for row in data_rows:
        cells_text = [row[c] if c < len(row) else None for c in data_surviving_cols]
        padded = cells_text + [None] * (n_display_cols - len(cells_text))
        cells = [f'<td style="vertical-align:top">{_fmt_cell(c)}</td>'
                 for c in padded[:n_display_cols]]
        lines.append('    <tr>' + ''.join(cells) + '</tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def render_tte_score_table(tdata: list, tier_labels: dict) -> str:
    """Render a tte_score table: wrapped 2-column header (label + score
    column, e.g. 'TTE (score/rating*)'), section-divider rows, and data
    rows (indication text + score/rating value)."""
    from html import escape as esc
    if not tdata:
        return ''

    def _fmt_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        if not parts:
            return ''
        _LIST_ITEM_RE = re.compile(r'^(?:[a-zA-Z]\.|[ivxIVX]+\.|\d+\.|\(\d+\)|•|\u2022)\s')
        out = [esc(parts[0])]
        for p in parts[1:]:
            if _LIST_ITEM_RE.match(p):
                out.append('<br>' + esc(p))
            else:
                out.append(' ' + esc(p))
        return ''.join(out)

    labels_in_order = ([tier_labels[i] for i in sorted(tier_labels)]
                        if tier_labels and len(tier_labels) == len(tdata)
                        else ['data'] * len(tdata))

    rows = [(lbl, row) for lbl, row in zip(labels_in_order, tdata)
            if row and any(c and str(c).strip() for c in row)]

    has_real_header_tag = any(lbl == 'header' for lbl, _ in rows)

    header_rows = []
    header_cols_used = set()
    body = []
    in_header = True
    for lbl, row in rows:
        nonempty_cols = {i for i, c in enumerate(row) if c and str(c).strip()}
        if in_header:
            if has_real_header_tag:
                if lbl == 'header':
                    header_rows.append(row)
                    continue
                else:
                    in_header = False
            else:
                if lbl == 'subheader':
                    if header_rows and not (nonempty_cols & header_cols_used):
                        in_header = False
                    else:
                        header_rows.append(row)
                        header_cols_used |= nonempty_cols
                        continue
                else:
                    in_header = False

        nonempty = [c for c in row if c and str(c).strip()]
        if len(nonempty) <= 1:
            body.append(('divider', row))
        else:
            body.append(('data', row))

    n_cols = max((len(r) for r in tdata if r), default=0)
    joined_header = []
    for c in range(n_cols):
        parts = [str(r[c]).strip().replace('\n', ' ') for r in header_rows
                 if r and c < len(r) and r[c] and str(r[c]).strip()]
        if parts:
            joined_header.append(esc(' '.join(parts)))
    if not joined_header:
        joined_header = ['']

    n_display_cols = len(joined_header)
    col_pct = 100 / n_display_cols if n_display_cols else 100
    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
    ]
    for _ in range(n_display_cols):
        lines.append(f'    <col style="width:{col_pct:.1f}%">')
    lines.append('   </colgroup>')
    lines.append('   <thead>')
    lines.append('    <tr style="background:#e8e8e8">')
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    # Build display rows: join consecutive 'subheader' rows into one
    # combined divider (same wrapped-phrase pattern as the header),
    # since a single divider label can also span multiple physical rows.
    display_rows = []  # list of ('divider'|'data', text_or_row)
    i = 0
    while i < len(body):
        kind, row = body[i]
        if kind == 'divider':
            parts = [str(c).strip() for c in row if c and str(c).strip()]
            j = i + 1
            while j < len(body) and body[j][0] == 'divider':
                parts.extend(str(c).strip() for c in body[j][1] if c and str(c).strip())
                j += 1
            display_rows.append(('divider', ' '.join(parts)))
            i = j
        else:
            display_rows.append(('data', row))
            i += 1

    for kind, item in display_rows:
        if kind == 'divider':
            lines.append(
                f'    <tr><td colspan="{n_display_cols}" '
                f'style="background:#ffffff;font-weight:bold">'
                f'{esc(item)}</td></tr>')
        else:
            row = item
            cells_text = [c for c in row if c and str(c).strip()]
            padded = cells_text + [None] * (n_display_cols - len(cells_text))
            cells = [f'<td style="vertical-align:top">{_fmt_cell(c)}</td>'
                     for c in padded[:n_display_cols]]
            lines.append('    <tr>' + ''.join(cells) + '</tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def _has_horizontal_rule_between(page, prev_row_bottom, cur_row_top, x0, x1, margin=1.5):
    """
    True if a horizontal rule (explicit page.lines entry, or a thin
    ruling rect) exists between prev_row_bottom and cur_row_top,
    spanning (most of) the row width. Used to distinguish a genuine
    section-divider boundary from a same-background-color row that's
    actually still part of a preceding multi-row header/title block --
    fill color alone can't tell these apart when both blocks share the
    same gray shade, but a real horizontal rule is a hard geometric
    fact independent of color.
    """
    y_lo = prev_row_bottom - margin
    y_hi = cur_row_top + margin
    if y_hi <= y_lo:
        return False

    min_width = (x1 - x0) * 0.5  # rule should span at least half the row width

    for l in page.lines:
        if not (x0 - margin <= l['x0'] <= x1 + margin and y_lo <= l['top'] <= y_hi):
            continue
        if abs(l['top'] - l['bottom']) < 1 and abs(l['x1'] - l['x0']) >= min_width:
            return True

    for r in page.rects:
        if not (x0 - margin <= r['x0'] <= x1 + margin and y_lo <= r['top'] <= y_hi):
            continue
        h = r['bottom'] - r['top']
        w = r['x1'] - r['x0']
        if h < 3 and w >= min_width:
            return True

    return False



def _consolidate_tte_score_table(t, tdata, page, is_continuation=False):
    """
    Like _consolidate_pref_criteria_table, but additionally splits a
    same-fill-color run of rows wherever a horizontal rule/border
    exists between two consecutive rows. Some tables (e.g. tte_score)
    use the identical background shade for both a title/header block
    and separate section-divider rows -- fill color alone can't tell
    these apart, but a real geometric rule is a hard, color-independent
    signal of a genuine row-group boundary.
    """
    if not tdata:
        return tdata, {}

    try:
        table_rows = t.rows
    except Exception:
        table_rows = None

    fills = []
    if table_rows and len(table_rows) == len(tdata):
        for row_obj in table_rows:
            fills.append(_row_fill_dominant(row_obj, page))
    else:
        fills = [None] * len(tdata)

    # Group consecutive rows with same fill into tiers, splitting a
    # same-fill run wherever a horizontal rule exists between rows.
    tiers = []
    for i, f in enumerate(fills):
        _split_here = False
        if (tiers and tiers[-1][0] == f and f is not None and table_rows and
                len(table_rows) == len(fills) and i > 0):
            _prev_row_obj = table_rows[i - 1]
            _cur_row_obj = table_rows[i]
            _tbl_x0 = min(r.bbox[0] for r in table_rows if r)
            _tbl_x1 = max(r.bbox[2] for r in table_rows if r)
            _split_here = _has_horizontal_rule_between(
                    page, _prev_row_obj.bbox[3], _cur_row_obj.bbox[1],
                    _tbl_x0, _tbl_x1)

        if tiers and tiers[-1][0] == f and not _split_here:
            tiers[-1][1].append(i)
        else:
            tiers.append((f, [i]))

    # Within each tier, find columns where at least one row has
    # non-empty content, and label the tier header/subheader/data.
    consolidated = []
    tier_labels = {}
    _header_assigned = is_continuation  # continuation fragments never have their own header
    for _fill, row_idxs in tiers:
        tier_rows = [tdata[i] for i in row_idxs]
        n_cols = max((len(r) for r in tier_rows if r), default=0)

        surviving_cols = [
            c for c in range(n_cols)
            if any((r[c] if r and c < len(r) else None) and
                   str(r[c]).strip() for r in tier_rows)
        ]

        if _fill is None:
            label = 'data'
        elif not _header_assigned:
            label = 'header'
            _header_assigned = True
        else:
            label = 'subheader'

        for idx in row_idxs:
            tier_labels[idx] = label

        for r in tier_rows:
            new_row = [r[c] if r and c < len(r) else None for c in surviving_cols]
            consolidated.append(new_row)

    return consolidated, tier_labels


def render_cancer_guidelines_table(tdata: list, tier_labels: dict) -> str:
    """Render a cancer_guidelines table: category label / long narrative
    guideline-citation text. Header row repeats 'Cancer' as column
    label; continuations repeat this same header."""
    from html import escape as esc
    if not tdata:
        return ''

    def _fmt_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        if not parts:
            return ''
        _LIST_ITEM_RE = re.compile(r'^(?:[a-zA-Z]\.|[ivxIVX]+\.|\d+\.|\(\d+\)|•|\u2022|"|o$)\s')
        out = [esc(parts[0])]
        for p in parts[1:]:
            if _LIST_ITEM_RE.match(p) or p.startswith('“') or p.startswith('"'):
                out.append('<br>' + esc(p))
            else:
                out.append(' ' + esc(p))
        return ''.join(out)

    def _fmt_label_cell(txt) -> str:
        if txt is None:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    labels_in_order = ([tier_labels[i] for i in sorted(tier_labels)]
                        if tier_labels and len(tier_labels) == len(tdata)
                        else ['data'] * len(tdata))
    rows = [(lbl, row) for lbl, row in zip(labels_in_order, tdata)
            if row and any(c and str(c).strip() for c in row)]

    header_rows = [row for lbl, row in rows if lbl == 'header']
    data_rows = [row for lbl, row in rows if lbl != 'header']

    n_cols = max((len(r) for r in tdata if r), default=0)
    header_col0 = ''
    header_col1 = 'Cancer'
    for r in header_rows:
        nonempty = [c for c in r if c and str(c).strip()]
        if nonempty:
            header_col1 = str(nonempty[0]).strip()

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:20%">',
        '    <col style="width:80%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
        f'     <th></th>',
        f'     <th>{esc(header_col1)}</th>',
        '    </tr>',
        '   </thead>',
        '   <tbody>',
    ]

    for row in data_rows:
        cells_text = [c for c in row if c and str(c).strip()]
        if not cells_text:
            continue
        if len(cells_text) == 1:
            # Category label with no narrative text on this row (rare);
            # or narrative-only continuation row -- put in col2, blank col1.
            lines.append(
                f'    <tr><td style="vertical-align:top"></td>'
                f'<td style="vertical-align:top">{_fmt_cell(cells_text[0])}</td></tr>')
        else:
            lines.append(
                f'    <tr><td style="vertical-align:top">{_fmt_label_cell(cells_text[0])}</td>'
                f'<td style="vertical-align:top">{_fmt_cell(cells_text[-1])}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def _rescue_hcpcs_cpt_icd_continuations(table_info: list, section_boundaries: list, 
                                          table_bboxes: set) -> None:
    """
    Post-classification rescue pass: a single-row (or few-row) table
    that structurally matches a code-table row (HCPCS/CPT/ICD) can get
    misclassified as spurious by _is_spurious_table (sparse content,
    few internal grid lines look like noise in isolation). If such a
    table sits immediately after a real hcpcs/cpt/icd/_continuation
    table -- same x0/x2, adjacent page, no real content in between --
    reclassify it as the matching _continuation type.

    Mutates table_info entries in place.
    """
    _kind_to_ttypes = {
        'hcpcs': ('hcpcs', 'hcpcs_continuation'),
        'cpt':   ('cpt', 'cpt_continuation'),
        'icd':   ('icd', 'icd_continuation'),
    }

    by_page = {}
    for e in table_info:
        by_page.setdefault(e['page'], []).append(e)

    for entry in table_info:
        if not str(entry.get('table_type', '')).startswith('spurious'):
            continue
        tdata = entry.get('tdata')
        if not tdata:
            continue

        _entry_sec = _section_at(section_boundaries, entry['page'], entry['bbox'][1])
        if 'coding' not in _entry_sec.lower():
            continue

        for kind, (fresh_ttype, cont_ttype) in _kind_to_ttypes.items():
            if not _matches_code_lineage(tdata, kind):
                continue

            # Look for the most recent real table of this kind, on the
            # immediately preceding page, with matching x0/x2 and no
            # other real (non-spurious) content between them.
            _cand_page = entry['page']
            _prev_entries = by_page.get(_cand_page - 1, [])
            _prev_match = next(
                (e for e in _prev_entries
                 if e['table_type'] in (fresh_ttype, cont_ttype) and
                 abs(e['bbox'][0] - entry['bbox'][0]) < 5 and
                 abs(e['bbox'][2] - entry['bbox'][2]) < 30 and 
                 'coding' in _section_at(section_boundaries, e['page'], e['bbox'][1]).lower()),
                None)

            if _prev_match is not None:
                entry['table_type'] = cont_ttype
                _en_top = entry.get('en_top', entry['bbox'][1])
                _en_bot = entry.get('en_bottom', entry['bbox'][3])
                table_bboxes.add((entry['page'], entry['bbox'][1], _en_top,
                                   entry['page'], entry['bbox'][3], _en_bot))
                break  # matched one kind, stop checking others


def _get_header1_group_bounds(page, header1_top, header1_bottom, header2_bottom,
                                col_grid, table_x0, table_x1):
    """
    Given the true column grid (from _get_true_column_grid), determine
    which grid boundaries also have a vertical edge spanning the FULL
    header height (header1 row through header2's bottom) -- these mark
    header1 category divisions. Returns the subset of col_grid that
    are group boundaries (always includes the outer table edges).
    """
    tf = page.debug_tablefinder()
    full_height_xs = set()
    for e in tf.edges:
        if (e.get('orientation') == 'v' and
                e['top'] <= header1_top + 2 and e['bottom'] >= header2_bottom - 2 and
                table_x0 - 2 <= e['x0'] <= table_x1 + 2):
            full_height_xs.add(round(e['x0'], 2))
    group_bounds = [x for x in col_grid
                    if any(abs(x - fx) < 1.5 for fx in full_height_xs)]
    if col_grid[0] not in group_bounds:
        group_bounds = [col_grid[0]] + group_bounds
    if col_grid[-1] not in group_bounds:
        group_bounds = group_bounds + [col_grid[-1]]
    return sorted(set(group_bounds))


def _remap_row_to_true_grid(row_cells, row_text, col_grid):
    """
    Map one row's raw cells onto the true column grid using strict,
    non-overlapping bin assignment (bisect-style) -- each x0 belongs
    to exactly one column band, avoiding the double-assignment that a
    symmetric tolerance pad on both bin edges would cause near shared
    boundaries.
    """
    import bisect
    n_true_cols = len(col_grid) - 1
    result = [None] * n_true_cols
    _eps = 0.75  # small rounding tolerance only, applied via left-shift

    for j, cell in enumerate(row_cells):
        if cell is None or j >= len(row_text):
            continue
        text = row_text[j]
        if not text or not str(text).strip():
            continue
        cx0 = cell[0] + _eps  # nudge right by eps before binning, to
                              # tolerate boundary-adjacent rounding
                              # without creating overlapping ranges
        idx = bisect.bisect_right(col_grid, cx0) - 1
        if idx < 0 or idx >= n_true_cols:
            continue
        if result[idx]:
            result[idx] = (str(result[idx]) + ' ' + str(text)).strip()
        else:
            result[idx] = text
    return result


def _consolidate_matrix_table(t, tdata, page):
    if not tdata:
        return tdata, {}

    try:
        table_rows = t.rows
    except Exception:
        table_rows = None

    fills = []
    if table_rows and len(table_rows) == len(tdata):
        for row_obj in table_rows:
            fills.append(_row_fill_dominant(row_obj, page))
    else:
        fills = [None] * len(tdata)

    tiers = []
    for i, f in enumerate(fills):
        if tiers and tiers[-1][0] == f:
            tiers[-1][1].append(i)
        else:
            tiers.append((f, [i]))

    tier_labels = {}
    _colored_tier_count = 0
    for _fill, row_idxs in tiers:
        if _fill is not None:
            _colored_tier_count += 1
            _base_label = 'header1' if _colored_tier_count == 1 else 'header2'
            for idx in row_idxs:
                tier_labels[idx] = _base_label
        else:
            for idx in row_idxs:
                _row = tdata[idx]
                _nonempty = [c for c in (_row or []) if c and str(c).strip()]
                tier_labels[idx] = 'subgroup' if len(_nonempty) <= 1 else 'data'

    _data_row_idxs = [i for i, lbl in tier_labels.items() if lbl == 'data']
    _header1_idxs = [i for i, lbl in tier_labels.items() if lbl == 'header1']
    _header2_idxs = [i for i, lbl in tier_labels.items() if lbl == 'header2']

    if table_rows and _data_row_idxs and _header1_idxs and _header2_idxs:
        try:
            _first_data_i = _data_row_idxs[0]
            _last_data_i = _data_row_idxs[-1]
            data_top = table_rows[_first_data_i].bbox[1]
            data_bottom = table_rows[_last_data_i].bbox[3]
            data_row_ranges = [
                (table_rows[i].bbox[1], table_rows[i].bbox[3])
                for i in _data_row_idxs if i < len(table_rows)
            ]
            col_grid = _get_true_column_grid(page, t, data_row_ranges, t.bbox[0], t.bbox[2])

            header1_top = table_rows[_header1_idxs[0]].bbox[1]
            header2_bottom = table_rows[_header2_idxs[-1]].bbox[3]
            header1_group_bounds = _get_header1_group_bounds(
                page, header1_top, header1_top, header2_bottom,
                col_grid, t.bbox[0], t.bbox[2])

            if col_grid and len(col_grid) >= 2:
                remapped = []
                for i, row in enumerate(tdata):
                    _lbl = tier_labels.get(i)
                    row_top = table_rows[i].bbox[1] if i < len(table_rows) else None
                    if i + 1 < len(table_rows):
                        row_bottom = table_rows[i + 1].bbox[1]
                    else:
                        row_bottom = table_rows[i].bbox[3] if i < len(table_rows) else None

                    if _lbl == 'header1':
                        remapped.append(_extract_header1_row_by_words(
                            page, row_top, row_bottom, header1_group_bounds, col_grid))
                    elif _lbl == 'subgroup':
                        remapped.append(_extract_subgroup_row_by_words(
                            page, row_top, row_bottom, len(col_grid) - 1))
                    elif _lbl == 'header2':
                        remapped.append(_extract_header_row_by_words(
                            page, row_top, row_bottom, col_grid))
                    else:
                        cells = table_rows[i].cells if i < len(table_rows) else None
                        if cells:
                            remapped.append(_remap_row_to_true_grid(cells, row, col_grid))
                        else:
                            remapped.append(row)

                # Merge multi-row header2 (e.g. 'nr-' + 'axSpA' wrapped
                # across two physical rows) into a single joined row,
                # column-wise, before returning.
                _h2_positions = [i for i, lbl in tier_labels.items() if lbl == 'header2']
                if len(_h2_positions) > 1:
                    _joined_h2 = list(remapped[_h2_positions[0]])
                    for _pos in _h2_positions[1:]:
                        _extra_row = remapped[_pos]
                        for _ci in range(len(_joined_h2)):
                            _extra_val = _extra_row[_ci] if _ci < len(_extra_row) else None
                            if _extra_val and str(_extra_val).strip():
                                _joined_h2[_ci] = (
                                    (str(_joined_h2[_ci]).strip() + ' ' + str(_extra_val).strip())
                                    if _joined_h2[_ci] and str(_joined_h2[_ci]).strip()
                                    else str(_extra_val).strip()
                                )
                    remapped[_h2_positions[0]] = _joined_h2
                    _drop_positions = set(_h2_positions[1:])
                    _new_remapped = []
                    _new_tier_labels = {}
                    _new_i = 0
                    for _old_i, _row in enumerate(remapped):
                        if _old_i in _drop_positions:
                            continue
                        _new_remapped.append(_row)
                        _new_tier_labels[_new_i] = tier_labels[_old_i]
                        _new_i += 1
                    remapped = _new_remapped
                    tier_labels = _new_tier_labels

                return remapped, tier_labels
        except Exception as ex:
            import traceback
            print(f"[DEBUG matrix_grid] FAILED: {ex}")
            traceback.print_exc()

    # ── FALLBACK: old surviving_cols behavior ──
    consolidated = []
    for _fill, row_idxs in tiers:
        tier_rows = [tdata[i] for i in row_idxs]
        n_cols = max((len(r) for r in tier_rows if r), default=0)
        surviving_cols = [
            c for c in range(n_cols)
            if any((r[c] if r and c < len(r) else None) and
                   str(r[c]).strip() for r in tier_rows)
        ]
        for r in tier_rows:
            new_row = [r[c] if r and c < len(r) else None for c in surviving_cols]
            consolidated.append(new_row)

    return consolidated, tier_labels


def _extract_header_row_by_words(page, row_top, row_bottom, col_grid):
    """
    Extract header2 row text directly from page.extract_words(),
    bypassing pdfplumber's own table-cell extraction (which can
    duplicate text into adjacent raw columns for a merged/wide header
    cell). Each word is assigned to a true column by its own x0, using
    bisect-style non-overlapping bin assignment against col_grid.
    Row y-range uses a tight tolerance and strict upper bound to avoid
    bleeding into an adjacent row when row spacing is very tight.
    """
    import bisect
    n_true_cols = len(col_grid) - 1
    result = [None] * n_true_cols
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    for w in words:
        if not (row_top - 0.2 <= w['top'] < row_bottom - 0.2):
            continue
        cx0 = w['x0'] + 0.75
        idx = bisect.bisect_right(col_grid, cx0) - 1
        if idx < 0 or idx >= n_true_cols:
            continue
        text = w['text']
        if result[idx]:
            result[idx] = (result[idx] + ' ' + text).strip()
        else:
            result[idx] = text
    return result


def _extract_header1_row_by_words(page, row_top, row_bottom, group_bounds, col_grid):
    """
    Extract a header1 (category-grouping) row's text, assigning each
    word to its GROUP (using group_bounds, the coarser header1-level
    boundaries) rather than the fine-grained per-column grid.
    """
    import bisect
    n_true_cols = len(col_grid) - 1
    n_groups = len(group_bounds) - 1
    group_words = [[] for _ in range(n_groups)]

    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    for w in words:
        if not (row_top - 0.2 <= w['top'] < row_bottom - 0.2):
            continue
        gx0 = w['x0'] + 0.75
        gidx = bisect.bisect_right(group_bounds, gx0) - 1
        if gidx < 0 or gidx >= n_groups:
            continue
        group_words[gidx].append(w['text'])

    result = [None] * n_true_cols
    for gidx in range(n_groups):
        if not group_words[gidx]:
            continue
        label = ' '.join(group_words[gidx])
        _first_col_x0 = group_bounds[gidx] + 0.75
        _target_col = bisect.bisect_right(col_grid, _first_col_x0) - 1
        if 0 <= _target_col < n_true_cols:
            result[_target_col] = label
    return result


def _extract_subgroup_row_by_words(page, row_top, row_bottom, n_true_cols):
    """
    Extract a subgroup divider row's text as ONE complete string
    spanning the whole row.
    """
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    row_words = [w for w in words if row_top - 0.2 <= w['top'] < row_bottom - 0.2]
    row_words.sort(key=lambda w: w['x0'])
    label = ' '.join(w['text'] for w in row_words).strip()
    result = [None] * n_true_cols
    if label:
        result[0] = label
    return result


def _get_true_column_grid(page, table, data_row_ranges, table_x0, table_x1):
    """
    Derive the true column grid using debug_tablefinder() vertical
    edges, checked against EACH individual data row's own y-range
    (data_row_ranges: list of (row_top, row_bottom) tuples) rather
    than requiring a single edge to span the entire data region
    continuously -- a subgroup divider row sitting between two data
    blocks breaks vertical lines into separate segments, so edges
    must be unioned per-row, not matched against one overall range.
    """
    tf = page.debug_tablefinder()
    xs = set()
    for e in tf.edges:
        if e.get('orientation') != 'v':
            continue
        if not (table_x0 - 2 <= e['x0'] <= table_x1 + 2):
            continue
        for (row_top, row_bottom) in data_row_ranges:
            if e['top'] <= row_top + 2 and e['bottom'] >= row_bottom - 2:
                xs.add(round(e['x0'], 2))
                break
    xs.add(round(table_x0, 2))
    xs.add(round(table_x1, 2))
    return sorted(xs)


def _normalize_plan_category(title: str) -> str:
    """
    Normalize a prod_criteria table's title into a plan_category value:
    strip trailing ':', a leading 'For ', normalize 'Plans' -> 'Plan'
    uniformly, and collapse a redundant mid-string 'Plan and' into
    plain 'and' when the title ends with its own 'Plan' (e.g. 'Employer
    Plan and Individual and Family Plan' -> 'Employer and Individual
    and Family Plan', matching the equivalent phrasing some documents
    use directly).
    """
    if not title:
        return ''
    t = title.strip()
    t = re.sub(r':\s*$', '', t)
    t = re.sub(r'^For\s+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bPlans\b', 'Plan', t, flags=re.IGNORECASE)
    t = re.sub(r'\bPlan\s+and\b', 'and', t, flags=re.IGNORECASE)
    return t.strip()


def consolidate_moa_rows(rows_with_labels: list) -> list[tuple[str, str, str]]:
    """
    Shared row-processing logic for moa (Mechanism of Action) tables.
    Given (label, row) pairs (header rows already excluded by caller),
    returns (drug_name, mechanism_of_action, indications) tuples.

    A row with an empty drug name AND empty MOA, but populated
    indications, is a continuation of the previous entry's indications
    column (e.g. 'SC formulation: ...' / 'IV formulation: ...' listed
    on separate rows for the same drug) -- appended to the previous
    entry's indications with a line break, not treated as a new row.
    """
    results: list[list[str]] = []
    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if not row:
            continue
        c0 = (row[0] or '').strip() if len(row) > 0 else ''
        _rest = [(row[i] or '').strip() for i in range(1, len(row)) if row[i] and str(row[i]).strip()]

        if not c0 and len(_rest) == 1 and results:
            prev = results[-1]
            prev[2] = (prev[2] + '\n' + _rest[0]) if prev[2] else _rest[0]
            continue

        if not c0 and not _rest:
            continue

        c1 = _rest[0] if len(_rest) > 0 else ''
        c2 = _rest[1] if len(_rest) > 1 else ''
        results.append([c0, c1, c2])

    return [(d, m, i) for d, m, i in results]


def render_moa_table_from_labeled(rows_with_labels: list) -> str:
    """
    Render a moa (Mechanism of Action) table: 3-column header (Drug /
    Mechanism of Action / Examples of Indications), possibly with a
    trailing category label (e.g. 'Biologics') as the last header row,
    and data rows (drug name, mechanism, indications -- indications
    may wrap across multiple rows for the same drug, e.g. separate
    'SC formulation:'/'IV formulation:' lines).
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_cell(txt) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']
    n_cols = 3
    joined_header = ['', '', '']
    for r in header_rows:
        _nonempty = [str(c).strip() for c in (r or []) if c and str(c).strip()]
        _start = max(0, n_cols - len(_nonempty))
        for i, val in enumerate(_nonempty[:n_cols]):
            _slot = _start + i
            joined_header[_slot] = (joined_header[_slot] + ' ' + val).strip() if joined_header[_slot] else val
    joined_header = [esc(h) for h in joined_header]

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:26%">',
        '    <col style="width:24%">',
        '    <col style="width:50%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
    ]
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    # Process body rows in order, handling subheader dividers inline
    # and consolidating consecutive data rows (wrap-around indications).
    _body_results: list[tuple[str, str, str] | str] = []  # str = divider text
    _pending_data_rows_with_labels = []

    def _flush_pending():
        for d, m, i in consolidate_moa_rows(_pending_data_rows_with_labels):
            _body_results.append((d, m, i))
        _pending_data_rows_with_labels.clear()

    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if lbl == 'subheader':
            _flush_pending()
            text = next((c for c in (row or []) if c and str(c).strip()), '')
            _body_results.append(str(text).strip())
            continue
        _pending_data_rows_with_labels.append((lbl, row))
    _flush_pending()

    for item in _body_results:
        if isinstance(item, str):
            lines.append(
                f'    <tr><td colspan="{n_cols}" '
                f'style="background:#ffffff;font-weight:bold">'
                f'{esc(item)}</td></tr>')
        else:
            drug_name, moa, indications = item
            lines.append(
                f'    <tr><td style="vertical-align:top">{_fmt_cell(drug_name)}</td>'
                f'<td style="vertical-align:top">{_fmt_cell(moa)}</td>'
                f'<td style="vertical-align:top">{_fmt_cell(indications)}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def extract_moa_category(header_rows: list) -> str | None:
    """
    If the LAST header-tagged row has exactly one nonempty cell,
    treat it as a category label (e.g. 'Biologics') for the whole
    table. Returns None if the header has no such trailing label.
    """
    if not header_rows:
        return None
    last_row = header_rows[-1]
    _nonempty = [c for c in (last_row or []) if c and str(c).strip()]
    if len(_nonempty) == 1:
        return str(_nonempty[0]).strip()
    return None


def extract_fda_device_mfg_rows(rows_with_labels: list) -> list[dict]:
    """
    Extract (device_or_product, identifier, manufacturer) tuples from
    fda_device_mfg rows_with_labels (label, row) pairs, header rows
    already excluded by caller. Uses position-independent extraction
    (first three nonempty values in row order) to be robust against
    phantom empty columns, which vary between documents.
    """
    results = []
    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if not row:
            continue
        _nonempty = [(str(c).strip()) for c in row if c and str(c).strip()]
        if not _nonempty:
            continue
        device = _nonempty[0] if len(_nonempty) > 0 else ''
        identifier = _nonempty[1] if len(_nonempty) > 1 else ''
        manufacturer = _nonempty[2] if len(_nonempty) > 2 else ''
        results.append({
            'device_or_product': device or None,
            'identifier': identifier or None,
            'manufacturer': manufacturer or None,
        })
    return results


def render_fda_device_mfg_table(rows_with_labels: list) -> str:
    """
    Render an fda_device_mfg table: 3-column header (Device or Product
    / Identifier / Manufacturer), single header row, data rows.
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_cell(txt) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']
    n_cols = 3
    joined_header = ['', '', '']
    for r in header_rows:
        _nonempty = [str(c).strip() for c in (r or []) if c and str(c).strip()]
        for i, val in enumerate(_nonempty[:n_cols]):
            joined_header[i] = (joined_header[i] + ' ' + val).strip() if joined_header[i] else val
    joined_header = [esc(h) for h in joined_header]

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:45%">',
        '    <col style="width:20%">',
        '    <col style="width:35%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
    ]
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    for d in extract_fda_device_mfg_rows(rows_with_labels):
        lines.append(
            f'    <tr><td style="vertical-align:top">{_fmt_cell(d["device_or_product"])}</td>'
            f'<td style="vertical-align:top">{_fmt_cell(d["identifier"])}</td>'
            f'<td style="vertical-align:top">{_fmt_cell(d["manufacturer"])}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def render_appendix_med_table(rows_with_labels: list) -> str:
    """
    Render an appendix_med table: 2-column header (Medication / Mode
    of Administration), single header row, plain data rows -- no
    subgroups, no wrap-around merging.
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_cell(txt) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']
    n_cols = 2
    joined_header = ['', '']
    for r in header_rows:
        _nonempty = [str(c).strip() for c in (r or []) if c and str(c).strip()]
        for i, val in enumerate(_nonempty[:n_cols]):
            joined_header[i] = (joined_header[i] + ' ' + val).strip() if joined_header[i] else val
    joined_header = [esc(h) for h in joined_header]

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:60%">',
        '    <col style="width:40%">',
        '   </colgroup>',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
    ]
    for h in joined_header:
        lines.append(f'     <th>{h}</th>')
    lines += ['    </tr>', '   </thead>', '   <tbody>']

    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if not row:
            continue
        _nonempty = [str(c).strip() for c in row if c and str(c).strip()]
        c0 = _nonempty[0] if len(_nonempty) > 0 else ''
        c1 = _nonempty[1] if len(_nonempty) > 1 else ''
        lines.append(
            f'    <tr><td style="vertical-align:top">{_fmt_cell(c0)}</td>'
            f'<td style="vertical-align:top">{_fmt_cell(c1)}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def consolidate_two_col_heading_rows(rows_with_labels: list) -> list[tuple[str, str, str]]:
    """
    Shared row-processing logic for two_col_heading tables.

    Given (label, row) pairs (header rows already excluded by caller),
    returns a list of (section_heading, col1_value, col2_value) tuples:
      - A 'subheader' row (possibly spanning 2 physical rows, e.g. a
        detailed note) becomes a new section heading, carried forward
        onto subsequent data rows. An entirely empty subheader row
        clears the section heading back to None.
      - A data row's nonempty cells are read in left-to-right order;
        the first is col1, the second (if any) is col2 -- this
        correctly handles both the 1-column leading section (before
        any subheading appears) and the standard 2-column case.
    """
    results = []
    _current_section = None
    _pending_subheader_text = None

    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if lbl == 'subheader':
            _nonempty = [str(c).strip() for c in (row or []) if c and str(c).strip()]
            _text = ' '.join(_nonempty) if _nonempty else ''
            if _pending_subheader_text is not None:
                _pending_subheader_text = (_pending_subheader_text + ' ' + _text).strip()
            else:
                _pending_subheader_text = _text
            continue

        if _pending_subheader_text is not None:
            _current_section = _pending_subheader_text or None
            _pending_subheader_text = None

        if not row:
            continue
        _nonempty = [str(c).strip() for c in row if c and str(c).strip()]
        if not _nonempty:
            continue
        c1 = _nonempty[0] if len(_nonempty) > 0 else ''
        c2 = _nonempty[1] if len(_nonempty) > 1 else ''
        results.append((_current_section, c1, c2))

    return results


def render_two_col_heading_table(rows_with_labels: list) -> str:
    """
    Render a two_col_heading table: optional column header (single
    column if the header has no genuine second-column content), and
    data rows in the body. 'subheader'-tagged rows render as their
    own gray-background divider row -- possibly with visible text
    (a section title), or entirely empty (a bare gray-background row
    marking a new section, with no label text at all).
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_cell(txt) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']
    n_cols = 2
    joined_header = ['', '']
    for r in header_rows:
        _nonempty = [str(c).strip() for c in (r or []) if c and str(c).strip()]
        for i, val in enumerate(_nonempty[:n_cols]):
            joined_header[i] = (joined_header[i] + ' ' + val).strip() if joined_header[i] else val
    joined_header = [esc(h) for h in joined_header]

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:50%">',
        '    <col style="width:50%">',
        '   </colgroup>',
    ]
    if any(joined_header):
        if joined_header[1]:
            lines += [
                '   <thead>',
                '    <tr style="background:#e8e8e8">',
                f'     <th>{joined_header[0]}</th>',
                f'     <th>{joined_header[1]}</th>',
                '    </tr>',
                '   </thead>',
            ]
        else:
            lines += [
                '   <thead>',
                '    <tr style="background:#e8e8e8">',
                f'     <th colspan="{n_cols}">{joined_header[0]}</th>',
                '    </tr>',
                '   </thead>',
            ]
    lines.append('   <tbody>')

    for lbl, row in rows_with_labels:
        if lbl == 'header':
            continue
        if lbl == 'subheader':
            _nonempty = [str(c).strip() for c in (row or []) if c and str(c).strip()]
            _text = esc(' '.join(_nonempty)) if _nonempty else '&nbsp;'
            lines.append(
                f'    <tr><td colspan="{n_cols}" '
                f'style="background:#e0e0e0;font-weight:bold">'
                f'{_text}</td></tr>')
            continue
        if not row:
            continue
        _row_nonempty = [str(c).strip() for c in row if c and str(c).strip()]
        if not _row_nonempty:
            continue
        c1 = _row_nonempty[0] if len(_row_nonempty) > 0 else ''
        c2 = _row_nonempty[1] if len(_row_nonempty) > 1 else ''
        if not c2:
            lines.append(
                f'    <tr><td colspan="{n_cols}" style="vertical-align:top">'
                f'{_fmt_cell(c1)}</td></tr>')
        else:
            lines.append(
                f'    <tr><td style="vertical-align:top">{_fmt_cell(c1)}</td>'
                f'<td style="vertical-align:top">{_fmt_cell(c2)}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)


def render_two_col_heading_table_old(rows_with_labels: list) -> str:
    """
    Render a two_col_heading table: optional column header, 'subheader'
    rows rendered as spanning section dividers, and data rows (1 or 2
    columns, depending on whether this section has a genuine second
    column populated anywhere).
    """
    from html import escape as esc
    if not rows_with_labels:
        return ''

    def _fmt_cell(txt) -> str:
        if not txt:
            return ''
        parts = [p.strip() for p in str(txt).split('\n') if p.strip()]
        return ' '.join(esc(p) for p in parts)

    header_rows = [r for lbl, r in rows_with_labels if lbl == 'header']
    n_cols = 2
    joined_header = ['', '']
    for r in header_rows:
        _nonempty = [str(c).strip() for c in (r or []) if c and str(c).strip()]
        for i, val in enumerate(_nonempty[:n_cols]):
            joined_header[i] = (joined_header[i] + ' ' + val).strip() if joined_header[i] else val
    joined_header = [esc(h) for h in joined_header]

    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <colgroup>',
        '    <col style="width:50%">',
        '    <col style="width:50%">',
        '   </colgroup>',
    ]
    if any(joined_header):
        if joined_header[1]:
            lines += [
                '   <thead>',
                '    <tr style="background:#e8e8e8">',
                f'     <th>{joined_header[0]}</th>',
                f'     <th>{joined_header[1]}</th>',
                '    </tr>',
                '   </thead>',
            ]
        else:
            lines += [
                '   <thead>',
                '    <tr style="background:#e8e8e8">',
                f'     <th colspan="{n_cols}">{joined_header[0]}</th>',
                '    </tr>',
                '   </thead>',
            ]
    lines.append('   <tbody>')

    _last_section = object()  # sentinel, guaranteed to differ from None on first row
    for section_heading, c1, c2 in consolidate_two_col_heading_rows(rows_with_labels):
        if section_heading != _last_section:
            if section_heading:
                lines.append(
                    f'    <tr><td colspan="{n_cols}" '
                    f'style="background:#ffffff;font-weight:bold">'
                    f'{esc(section_heading)}</td></tr>')
            _last_section = section_heading
        if not c2:
            lines.append(
                f'    <tr><td colspan="{n_cols}" style="vertical-align:top">'
                f'{_fmt_cell(c1)}</td></tr>')
        else:
            lines.append(
                f'    <tr><td style="vertical-align:top">{_fmt_cell(c1)}</td>'
                f'<td style="vertical-align:top">{_fmt_cell(c2)}</td></tr>')

    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)



def _has_horizontal_line_above(row_obj, page, tolerance=2.0):
    """
    Check whether a horizontal line or thin rectangle exists on the
    page immediately above this row's top edge (within `tolerance`
    points) -- pdfplumber represents ruled lines inconsistently
    across documents, sometimes as page.lines, sometimes as very
    thin page.rects, so both are checked.
    """
    try:
        _row_top = row_obj.bbox[1]
    except Exception:
        return False

    for ln in (page.lines or []):
        _ln_y = ln.get('top', ln.get('y0'))
        if _ln_y is not None and abs(_ln_y - _row_top) <= tolerance:
            return True

    for rc in (page.rects or []):
        _height = abs(rc.get('bottom', 0) - rc.get('top', 0))
        if _height <= 1.5:  # thin rect used as a rule line
            _rc_top = rc.get('top')
            if _rc_top is not None and abs(_rc_top - _row_top) <= tolerance:
                return True

    return False


def _consolidate_two_col_heading_table(t, tdata, page, _seen_data_before=False):
    """
    two_col_heading-specific consolidation: same fill-based tiering as
    _consolidate_pref_criteria_table for the header/subheader/data
    distinction, PLUS a line-detection pass that splits the leading
    header block wherever a horizontal rule appears between rows --
    two_col_heading tables can have a genuine table-level header
    followed immediately (no data row in between) by a section
    subheading, distinguished only by a ruled line, not by a fill-
    color or row-position change.

    `_seen_data_before`: whether a genuine data row has already
    occurred earlier in this table's lineage (on a previous page or
    earlier in this same page's processing) -- needed because a
    repeated header/note row can appear at the top of a continuation
    page, and should be reclassified as 'subheader' (not 'header')
    once the table has moved past its own leading header block,
    even though this function only ever sees one page's tdata at a
    time and has no memory of prior pages on its own.
    """
    if not tdata:
        return tdata, {}, _seen_data_before

    try:
        table_rows = t.rows
    except Exception:
        table_rows = None

    fills = []
    if table_rows and len(table_rows) == len(tdata):
        for _i, row_obj in enumerate(table_rows):
            _f = _row_fill(row_obj, page)
            fills.append(_f)
    else:
        fills = [None] * len(tdata)
        table_rows = None

    tiers = []
    for i, f in enumerate(fills):
        if tiers and tiers[-1][0] == f:
            tiers[-1][1].append(i)
        else:
            tiers.append((f, [i]))

    # Compute a single, global surviving-columns set from ALL rows
    # (not per-tier) -- an isolated single-row tier can otherwise
    # derive a spurious, row-specific column mapping from its own one
    # nonempty cell, losing alignment with the table's true columns.
    _global_n_cols = max((len(r) for r in tdata if r), default=0)
    _global_surviving_cols = [
        c for c in range(_global_n_cols)
        if any((r[c] if r and c < len(r) else None) and
               str(r[c]).strip() for r in tdata)
    ]

    consolidated = []
    tier_labels = {}
    _seen_data = _seen_data_before
    for _fill, row_idxs in tiers:
        tier_rows = [tdata[i] for i in row_idxs]
        n_cols = max((len(r) for r in tier_rows if r), default=0)

        surviving_cols = [
            c for c in range(n_cols)
            if any((r[c] if r and c < len(r) else None) and
                   str(r[c]).strip() for r in tier_rows)
        ]

        if _fill is not None and 0.7 <= _fill < 0.92:
            label = 'header'
        elif _fill is not None and 0.92 <= _fill < 1.0:
            label = 'subheader'
        else:
            label = 'data'

        for _pos, idx in enumerate(row_idxs):
            _this_label = label
            if label == 'header' and _pos > 0 and table_rows and idx < len(table_rows):
                if _has_horizontal_line_above(table_rows[idx], page):
                    _this_label = 'subheader'
            if _this_label == 'data':
                _seen_data = True
            elif _this_label == 'header' and _seen_data:
                _this_label = 'subheader'
            tier_labels[idx] = _this_label

        for r in tier_rows:
            new_row = [r[c] if r and c < len(r) else None for c in _global_surviving_cols]
            consolidated.append(new_row)

    return consolidated, tier_labels, _seen_data


def _has_intervening_content(raw_lines, table_bboxes,
                              start_page, start_bottom, end_page, end_top):
    def _in_any_table(pg, top):
        for sp, st, en_st, ep, eb, en_eb in table_bboxes:
            if sp == ep:
                if pg == sp and en_st <= top <= en_eb:
                    return True
            else:
                if pg == sp and top >= en_st:
                    return True
                if pg == ep and top <= en_eb:
                    return True
                if sp < pg < ep:
                    return True
        return False

    for l in raw_lines:
        pg = l.get('page')
        if pg is None or pg < start_page or pg > end_page:
            continue
        if pg == start_page and l['top'] <= start_bottom:
            continue
        if pg == end_page and l['top'] >= end_top:
            continue
        if not l['text'].strip():
            continue
        if _in_any_table(pg, l['top']):
            continue
        return True
    return False
