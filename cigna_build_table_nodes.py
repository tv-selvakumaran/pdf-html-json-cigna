#!/usr/bin/env python3
"""
cigna_build_table_nodes.py
==========================
Table type classification and HTML construction for the Cigna PDF pipeline.

Contains:
  - All _try_*_table handler functions
  - build_table_nodes() dispatcher

Depends on cigna_parse_tables.py for:
  - _section_at, render_generic_table, _is_spurious_table, _true_table_top
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pdfplumber

from cigna_parse_nodes import (
    TableNode,
    SECTION_VOCAB,
    MM_SECTION_VOCAB,
)

from cigna_parse_headings import classify_section

from cigna_parse_tables import (
    _section_at,
    render_generic_table,
    _is_spurious_table,
    _true_table_top,
)

try:
    from reconstruct_cigna_table import (
        reconstruct_drug_quantity_table,
        reconstruct_availability_table,
        reconstruct_fda_dosing_table,
        reconstruct_moa_table,
        reconstruct_revision_table,
    ) 
    HAS_RECONSTRUCTORS = True
except ImportError:
    HAS_RECONSTRUCTORS = False


def _try_dql_table(t, tdata, row0, pi, pages, page,
                   seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    def _has_product_col0(td):
        if not td[0]:
            return False
        has_product = any(
            (c or '').strip() in ('Product', 'Product Name')
            for c in td[0])
        if not has_product:
            return False
        # Only a genuine DQL table if row0 also contains at least one
        # quantity/strength/limit keyword. Tables with 'Criteria',
        # 'Mechanism of Action', 'Drug Availability', 'Number of
        # injections', etc. are criteria/generic tables, not DQL.
        _dql_keywords = {
            'strength', 'retail', 'home', 'delivery', 'maximum',
            'quantity', 'limit', 'limits', 'days', 'supply', 'dosage',
        }
        _flat_row0_lower = ' '.join((c or '').lower() for c in td[0])
        _row0_words = set(_flat_row0_lower.split())
        return bool(_row0_words & _dql_keywords)

    if not _has_product_col0(tdata):
        return None

    p2data = None
    if pi + 1 < len(pages):
        p2tbls = pages[pi + 1].find_tables()
        if p2tbls:
            _p2_bbox_dql = p2tbls[0].bbox
            _p2_page = pages[pi + 1]
            _p2_words_above = [
                w for w in _p2_page.extract_words()
                if w['top'] < _p2_bbox_dql[1] - 5
            ]
            if not _p2_words_above:
                _dql_section = _section_at(
                    section_boundaries, pi + 1, t.bbox[1])
                _cont_section = _section_at(
                    section_boundaries, pi + 2, _p2_bbox_dql[1])
                if _dql_section == _cont_section:
                    p2data = p2tbls[0].extract()
                    seen_bboxes.add((pi + 1,
                        round(_p2_bbox_dql[0]),
                        round(_p2_bbox_dql[1]),
                        round(_p2_bbox_dql[2]),
                        round(_p2_bbox_dql[3])))

    _dql_gray = None
    for _r in page.rects:
        _col = _r.get('non_stroking_color', 0)
        if isinstance(_col, (list, tuple)):
            _col = sum(_col)/len(_col) if _col else 0
        if (_r.get('fill') and 0.3 <= float(_col) <= 0.98 and
                _r['top'] >= t.bbox[1] - 2 and
                _r['bottom'] <= t.bbox[3] + 2):
            if _dql_gray is None or _r['bottom'] > _dql_gray:
                _dql_gray = _r['bottom']

        html = reconstruct_drug_quantity_table(tdata, p2data, gray_bottom=_dql_gray)
        if html:
            return TableNode(html=html, table_type='dql', page=pi+1, top=t.bbox[1])
        return None


def _try_revision_table(t, tdata, row0, pi, pages, page,
                   seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    combined_rev = list(tdata)
    _rev_bbox = t.bbox
    _rev_section = _section_at(section_boundaries, pi + 1, t.bbox[1])
    _npi_rev = pi + 1
    while _npi_rev < len(pages):
        p2tbls = pages[_npi_rev].find_tables()
        if not p2tbls:
            break
        next_tdata = p2tbls[0].extract() or []
        next_row0 = next_tdata[0] if next_tdata else []
        _next_c0 = (next_row0[0] if next_row0 else '') or ''
        _next_flat = ' '.join(str(c) for c in next_row0 if c)
        _is_other_type = bool(
            'Mechanism of Action' in ' '.join(
                str(c) for row in (next_tdata[:3] or [])
                for c in (row or []) if c) or
            'HCPCS' in _next_flat or
            'CPT' in _next_flat or
            _next_c0.strip() == 'Product')
        _nxt_words = [w for w in pages[_npi_rev].extract_words()
                      if w['top'] < p2tbls[0].bbox[1] - 5]
        if (not _is_other_type and
                len(next_row0) <= 3 and
                not _nxt_words):
            # Section boundary check — revision table cannot span into a different section
            _cont_section = _section_at(
                section_boundaries, _npi_rev + 1, p2tbls[0].bbox[1])
            if _cont_section != _rev_section:
                break
            combined_rev.extend(next_tdata)
            _p2_bbox_rev = p2tbls[0].bbox
            seen_bboxes.add((_npi_rev,
                round(_p2_bbox_rev[0]), round(_p2_bbox_rev[1]),
                round(_p2_bbox_rev[2]), round(_p2_bbox_rev[3])))
            _npi_rev += 1
        else:
            break

    html = reconstruct_revision_table(combined_rev, None)
    if html:
        return TableNode(html=html, table_type='revision',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_fda_dosing_table(t, tdata, row0, pi, pages, page,
                          seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    _t_lc = title.lower()

    # Detected by title containing 'FDA-Approved Dosing' or 'FDA- Dosing'
    # and multiline header with None in col0
    _is_fda_dosing = (
        'fda' in _t_lc and 'dosing' in _t_lc and
        any((row[0] is None) for row in tdata[:3])
    )
    if not _is_fda_dosing:
        return None

    # Collect continuation page
    _p2_dosing = None
    _p2_page_dosing = None
    _ct_d = None
    if pi + 1 < len(pages):
        _ct_d = pages[pi + 1].find_tables()
    if _ct_d:
        _cb_d = _ct_d[0].bbox
        _nxt_words_d = [w for w in pages[pi+1].extract_words()
                        if w['top'] < _cb_d[1] - 5]
        if (abs(_cb_d[0] - t.bbox[0]) < 10 and
                _cb_d[1] < 140 and not _nxt_words_d):
            _p2_dosing = _ct_d[0].extract()
            seen_bboxes.add((pi + 1, round(_cb_d[0]),
                round(_cb_d[1]), round(_cb_d[2]), round(_cb_d[3])))

    # Detect gray_bottom
    _dos_gray = None
    for _r in page.rects:
        _col = _r.get('non_stroking_color', 0)
        if isinstance(_col, (list, tuple)):
            _col = sum(_col) / len(_col) if _col else 0
        if (_r.get('fill') and 0.3 <= float(_col) <= 0.98 and
                _r['top'] >= t.bbox[1] - 2 and
                _r['bottom'] <= t.bbox[3] + 2):
            if _dos_gray is None or _r['bottom'] > _dos_gray:
                _dos_gray = _r['bottom']

    _p2_page_dosing = pages[pi + 1] if _p2_dosing is not None else None
    html = reconstruct_fda_dosing_table(
        tdata, _p2_dosing,
        gray_bottom=_dos_gray,
        p1_page=page,
        p2_page=_p2_page_dosing
    )
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_titled_generic_table(t, tdata, row0, pi, pages, page,
                              seen_bboxes, section_boundaries,
                              title) -> 'TableNode | None':
    _t_norm = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', title)
    _t_lc = _t_norm.lower()

    _is_known_title = (
        re.match(r'(Appendix\s+)?Table\s+\d+[.]', _t_norm) or
        'preferred and non-preferred' in _t_lc or
        'preferred products' in _t_lc or
        'by indication' in _t_lc or
        'drug availability' in _t_lc or
        'dosage forms' in _t_lc or
        'fda approved' in _t_lc or
        'fda recommended' in _t_lc or
        'simon broome' in _t_lc or
        'dutch lipid' in _t_lc or
        'laboratory diagnosis' in _t_lc or
        'diagnostic criteria' in _t_lc or
        'reauthorization criteria' in _t_lc or
        'dose conversion' in _t_lc or
        'dosing regimen' in _t_lc or
        'indications' in _t_lc or
        'individual and family plans' in _t_lc or
        'employer plans' in _t_lc or
        'fda approved indication' in _t_lc or
        'fda recommended dosing' in _t_lc or
        'fda approved products' in _t_lc or
        _t_lc.strip() in ('dosing', 'drug availability',
                          'dosage forms for this indication',
                          'follistim pen dose conversion table*') or
        re.match(r'Table\s+\d+[:\s]', _t_norm) is not None
    )
    if not _is_known_title:
        return None

    # Check if row0 is bold (has header)
    _row0_words = page.extract_words(extra_attrs=['fontname'])
    _row0_bold = any('Bold' in w.get('fontname','')
                     for w in _row0_words
                     if t.bbox[1] <= w['top'] <= t.bbox[1]+20
                     and t.bbox[0] <= w['x0'] <= t.bbox[2])
    _gen_data = list(tdata)
    if _row0_bold:
        # Collect multi-page continuation — must stay within the same section
        _this_section = _section_at(section_boundaries, pi + 1, t.bbox[1])
        _npi2 = pi + 1
        while _npi2 < len(pages):
            _ct2 = pages[_npi2].find_tables()
            if not _ct2: break
            _cb2 = _ct2[0].bbox
            _nxt_page = pages[_npi2]
            _nxt_words_above = [
                w for w in _nxt_page.extract_words()
                if w['top'] < _cb2[1] - 5
            ]
            if (abs(_cb2[0]-t.bbox[0]) < 10 and
                    _cb2[1] < 140 and
                    not _nxt_words_above):
                _cont_section = _section_at(
                    section_boundaries, _npi2 + 1, _cb2[1])
                if _cont_section != _this_section:
                    break
                _gen_data.extend(_ct2[0].extract() or [])
                seen_bboxes.add((_npi2, round(_cb2[0]),
                    round(_cb2[1]), round(_cb2[2]), round(_cb2[3])))
                _npi2 += 1
            else:
                break
    html = render_generic_table(_gen_data, has_header=_row0_bold)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_moa_table(t, tdata, row0, pi, pages, page,
                   seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_rows = ' '.join(
        (c or '').strip() for row in (tdata[:3] or [])
        for c in (row or []) if (c or '').strip())
    if not ('Mechanism of Action' in flat_rows and
            (row0[0] or '').strip() == ''):
        return None

    gray_bottom = None
    for r in page.rects:
        color = r.get('non_stroking_color', 0)
        if isinstance(color, (list, tuple)):
            color = sum(color) / len(color) if color else 0
        if (r.get('fill') and 0.7 <= float(color) <= 0.98 and
                r['top'] >= t.bbox[1] - 5 and
                r['bottom'] <= t.bbox[3] + 5):
            if gray_bottom is None or r['bottom'] > gray_bottom:
                gray_bottom = r['bottom']

    combined = list(tdata)
    moa_bbox = t.bbox
    moa_ncols = len(row0)
    _moa_section = _section_at(section_boundaries, pi + 1, t.bbox[1])
    next_pi = pi + 1
    while next_pi < len(pages):
        cont_tables = pages[next_pi].find_tables()
        if not cont_tables:
            break
        cont_bbox = cont_tables[0].bbox
        cont_data = cont_tables[0].extract() or []
        cont_row0 = cont_data[0] if cont_data else []
        # MOA header rows use 8-col merged layout; data rows use 3-col layout.
        # Accept continuation if it has 3 cols (data) OR matches header ncols.
        _cont_ncols = len(cont_row0)
        _ncols_ok = (_cont_ncols == moa_ncols or _cont_ncols == 3)
        if (abs(cont_bbox[0] - moa_bbox[0]) < 10 and
                cont_bbox[1] < 120 and
                _ncols_ok):
            # Verify continuation is in the same section
            _cont_section = _section_at(
                section_boundaries, next_pi + 1, cont_bbox[1])
            if _cont_section != _moa_section:
                break
            combined.extend(cont_data)
            seen_bboxes.add((next_pi, round(cont_bbox[0]),
                            round(cont_bbox[1]),
                            round(cont_bbox[2]),
                            round(cont_bbox[3])))
            next_pi += 1
        else:
            break
    html = reconstruct_moa_table(combined, gray_bottom=gray_bottom)
    if html:
        return TableNode(html=html, table_type='moa',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_known_kw_table(t, tdata, row0, pi, pages, page,
                        seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    _flat_r0_kw = ' '.join(str(c) for c in (row0 or []))
    if not any(kw in _flat_r0_kw for kw in (
            'Compound Name', 'Drug Name', 'Comments',
            'Prescribing Information', 'Ingredient')):
        return None

    _kw_words = page.extract_words(extra_attrs=['fontname'])
    _kw_bold = any('Bold' in w.get('fontname','')
                   for w in _kw_words
                   if t.bbox[1] <= w['top'] <= t.bbox[1]+20
                   and t.bbox[0] <= w['x0'] <= t.bbox[2])
    _kw_data = list(tdata)
    if _kw_bold:
        _this_section = _section_at(section_boundaries, pi + 1, t.bbox[1])
        _npi3 = pi + 1
        while _npi3 < len(pages):
            _ct3 = pages[_npi3].find_tables()
            if not _ct3: break
            _cb3 = _ct3[0].bbox
            _nxt_words_above3 = [
                w for w in pages[_npi3].extract_words()
                if w['top'] < _cb3[1] - 5
            ]
            if (abs(_cb3[0]-t.bbox[0]) < 10 and
                    _cb3[1] < 140 and
                    not _nxt_words_above3):
                _cont_section = _section_at(
                    section_boundaries, _npi3 + 1, _cb3[1])
                if _cont_section != _this_section:
                    break
                _kw_data.extend(_ct3[0].extract() or [])
                seen_bboxes.add((_npi3, round(_cb3[0]),
                    round(_cb3[1]), round(_cb3[2]), round(_cb3[3])))
                _npi3 += 1
            else:
                break

    html = render_generic_table(_kw_data, has_header=_kw_bold)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_availability_table(t, tdata, row0, pi, pages, page,
                            seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    tiny_by_x: dict[int, list] = defaultdict(list)
    tiny_words = page.extract_words(extra_attrs=['size'], x_tolerance=2, y_tolerance=2)
    for w in tiny_words:
        if w['size'] < 8.0 and len(w['text']) <= 3:
            xk = round(w['x0'] / 8) * 8
            tiny_by_x[xk].append(w)
    if sum(1 for ws in tiny_by_x.values() if len(ws) >= 4) < 4:
        return None
    has_product = any((c or '').strip() == 'Product' for c in (row0 or []))
    if has_product:
        return None
    html = reconstruct_availability_table(tdata)
    if html:
        return TableNode(html=html, table_type='availability',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_criteria_table(t, tdata, row0, pi, pages, page,
                        seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    has_criteria_col = any((c or '').strip() == 'Criteria' for c in (row0 or []))
    has_product_any  = any((c or '').strip() == 'Product' for c in (row0 or []))
    _is_dql_like = any((c or '').strip() in ('Product', 'Product Name')
                       for c in (row0 or []))
    if _is_dql_like or not (has_criteria_col or has_product_any):
        return None
    _crit_combined = list(tdata)
    _crit_bbox = t.bbox
    _this_section = _section_at(section_boundaries, pi + 1, t.bbox[1])
    _npi = pi + 1
    while _npi < len(pages):
        _ct = pages[_npi].find_tables()
        if not _ct: break
        _cb = _ct[0].bbox
        if (abs(_cb[0] - _crit_bbox[0]) < 10 and _cb[1] < 140):
            _cont_section = _section_at(section_boundaries, _npi + 1, _cb[1])
            if _cont_section != _this_section:
                break
            _crit_combined.extend(_ct[0].extract() or [])
            seen_bboxes.add((_npi, round(_cb[0]), round(_cb[1]),
                             round(_cb[2]), round(_cb[3])))
            _npi += 1
        else:
            break
    html = render_generic_table(_crit_combined)
    if html:
        return TableNode(html=html, table_type='criteria',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_hcpcs_table(t, tdata, row0, pi, pages, page,
                     seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    is_hcpcs = any('HCPCS' in (c or '') or 'CPT' in (c or '')
                   for c in (row0[:2] or []))
    is_icd = bool(row0 and 'ICD-10' in (row0[0] or ''))
    if not (is_hcpcs or is_icd):
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='hcpcs',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_eua_table(t, tdata, row0, pi, pages, page,
                   seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    is_eua = bool(len(row0) >= 2 and
                  (row0[0] or '').strip() == 'Date' and
                  'EUA' in (row0[1] or ''))
    if not is_eua:
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_fda_rx_table(t, tdata, row0, pi, pages, page,
                      seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    if not ('Drug' in flat_r0 and 'Prescribing' in flat_r0):
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_rev_flex_table(t, tdata, row0, pi, pages, page,
                        seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    first = (row0[0] or '').strip() if row0 else ''
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    is_rev_flex = (first != 'Type of Revision' and row0 and
                   any('Summary of Changes' in (c or '') for c in row0))
    if not is_rev_flex:
        return None
    p2data = None
    if pi + 1 < len(pages):
        p2tbls = pages[pi + 1].find_tables()
        if p2tbls:
            p2data = p2tbls[0].extract()
            _p2_bbox_flex = p2tbls[0].bbox
            seen_bboxes.add((pi + 1,
                round(_p2_bbox_flex[0]), round(_p2_bbox_flex[1]),
                round(_p2_bbox_flex[2]), round(_p2_bbox_flex[3])))
    html = reconstruct_revision_table(tdata, p2data)
    if html:
        return TableNode(html=html, table_type='revision',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_nonpref_table(t, tdata, row0, pi, pages, page,
                       seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    if not ('Exception Criteria' in flat_r0 or
            ('Non-Preferred' in flat_r0 and 'Criteria' in flat_r0) or
            'Criteria for Use' in flat_r0):
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_drug_equiv_table(t, tdata, row0, pi, pages, page,
                          seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    if not ('Non-Covered Brand' in flat_r0 or 'Bioequivalent' in flat_r0):
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_medication_moa_table(t, tdata, row0, pi, pages, page,
                               seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    flat_r0r2 = ' '.join(str(c) for row in (tdata[:3] or []) for c in (row or []))
    if not ('Medication' in flat_r0 and 'Mode of Administration' in flat_r0r2):
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_condition_criteria_table(t, tdata, row0, pi, pages, page,
                                   seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    flat_r0 = ' '.join(str(c) for c in (row0 or []))
    flat_r0r2 = ' '.join(str(c) for row in (tdata[:3] or []) for c in (row or []))
    if not ('Condition' in flat_r0 and 'Criteria for Use' in flat_r0r2):
        return None
    html = render_generic_table(tdata)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def _try_catch_all_table(t, tdata, row0, pi, pages, page,
                         seen_bboxes, section_boundaries, title) -> 'TableNode | None':
    _ca_words = page.extract_words(extra_attrs=['fontname'])
    _ca_bold = any('Bold' in w.get('fontname','')
                   for w in _ca_words
                   if t.bbox[1] <= w['top'] <= t.bbox[1]+20
                   and t.bbox[0] <= w['x0'] <= t.bbox[2])
    html = render_generic_table(tdata, has_header=_ca_bold)
    if html:
        return TableNode(html=html, table_type='generic',
                         page=pi+1, top=t.bbox[1], title=title)
    return None


def build_table_nodes(pdf_path: Path, para_cache: dict = None, 
                      table_info: list = None, section_boundaries: list = None,
                      section_vocab: set = None) -> list[TableNode]:
    """Extract and reconstruct all tables from the PDF.
    
    para_cache: optional dict from extract_paragraph_lines for title lookup.
    """

    def _section_at(page: int, top: float) -> str:
        if not section_boundaries:
            return ''
        active = ''
        for b in section_boundaries:
            if b['page'] < page or (b['page'] == page and b['top'] <= top):
                active = b['segment']
            else:
                break
        return active

    if not HAS_RECONSTRUCTORS:
        return []
    nodes = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = pdf.pages
        seen_bboxes: set = set()
        for pi, page in enumerate(pages):
            page_bboxes_build = [t2.bbox for t2 in page.find_tables()]
            for t in page.find_tables():
                bbox_key = (pi, round(t.bbox[0]), round(t.bbox[1]),
                            round(t.bbox[2]), round(t.bbox[3]))
                if bbox_key in seen_bboxes:
                    continue
                seen_bboxes.add(bbox_key)
                tdata = t.extract()
                # if not tdata or len(tdata) < 2:
                #     continue

                if not tdata:
                    continue
                if len(tdata) < 2:
                    # Allow 1-row tables only if they have 2+ non-empty columns
                    # (genuine single-entry tables, e.g. ph_8007 p2).
                    # Reject everything else — single-row, single-col or empty = artefact.
                    _nonempty = sum(1 for c in (tdata[0] or []) if (c or '').strip())
                    if _nonempty < 2:
                        continue

                # Skip spurious tables early
                if _is_spurious_table(tdata, t.bbox, page_bboxes_build, page=page):
                    continue

                # Skip tables in References section — citations are body text
                _tbl_section = _section_at(pi + 1, t.bbox[1])
                if _tbl_section and 'reference' in _tbl_section.lower():
                    continue

                row0  = tdata[0]
                first = (row0[0] or '').strip() if row0 else ''

                # ── Compute table title once, used by all handlers ────────
                _t_top = t.bbox[1]
                _t_title = ''
                if para_cache is not None:
                    _page_lines = para_cache.get(pi + 1, [])
                    _above2 = sorted(
                        [l for l in _page_lines if l['top'] < _t_top],
                        key=lambda l: -l['top'])
                    _coll2 = []
                    _prev2 = _t_top
                    _title_size = None  # size of the first (closest) title line
                    for _l2 in _above2:
                        if _prev2 - _l2['top'] > 20:
                            break
                        if _l2.get('bold', False):
                            _l2_size = _l2.get('size', 0)
                            if _title_size is None:
                                _title_size = _l2_size
                            elif abs(_l2_size - _title_size) > 1.0:
                                break
                            from cigna_parse_nodes import SECTION_VOCAB
                            _vocab = section_vocab if section_vocab else SECTION_VOCAB
                            if classify_section(_l2, vocab=_vocab):
                                break
                            _coll2.append(_l2)
                            _prev2 = _l2['top']
                        else:
                            break
                    _title_ls = sorted(_coll2,
                        key=lambda l: (round(l['top'] / 6) * 6,
                                       l.get('x0', 0)))
                    _t_title = ' '.join(l['text'] for l in _title_ls)
                else:
                    _words_pg = page.extract_words(extra_attrs=['fontname', 'size'])
                    _title_ws = [w for w in _words_pg
                                 if _t_top - 16 <= w['top'] < _t_top
                                 and 'Bold' in w.get('fontname', '')]
                    _t_title = ' '.join(w['text'] for w in _title_ws)

                # ── Revision table ────────────────────────────────────────
                if first == 'Type of Revision':
                    result = _try_revision_table(t, tdata, row0, pi, pages, page,
                                        seen_bboxes, section_boundaries, _t_title)
                    if result:
                        nodes.append(result)
                        continue


                # ── FDA-Approved Dosing table ─────────────────────────────────────
                result = _try_fda_dosing_table(t, tdata, row0, pi, pages, page,
                                               seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── DQL table ─────────────────────────────────────────────
                result = _try_dql_table(t, tdata, row0, pi, pages, page,
                                        seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Availability table ────────────────────────────────────
                result = _try_availability_table(t, tdata, row0, pi, pages, page,
                                                 seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Criteria table ────────────────────────────────────────
                result = _try_criteria_table(t, tdata, row0, pi, pages, page,
                                             seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── HCPCS / CPT / ICD-10 coding table ────────────────────
                result = _try_hcpcs_table(t, tdata, row0, pi, pages, page,
                                          seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue

 
                # ── EUA Letter table ──────────────────────────────────────
                result = _try_eua_table(t, tdata, row0, pi, pages, page,
                                        seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── FDA Prescribing Information table ─────────────────────
                result = _try_fda_rx_table(t, tdata, row0, pi, pages, page,
                                           seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Flexible revision table ───────────────────────────────
                result = _try_rev_flex_table(t, tdata, row0, pi, pages, page,
                                             seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Non-Preferred/Exception Criteria table ────────────────
                result = _try_nonpref_table(t, tdata, row0, pi, pages, page,
                                            seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Drug Equivalent table (Non-Covered Brand / Bioequivalent) ─
                result = _try_drug_equiv_table(t, tdata, row0, pi, pages, page,
                                               seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Medication/Mode of Administration table ──────────────
                result = _try_medication_moa_table(t, tdata, row0, pi, pages, page,
                                                   seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Condition/Criteria for Use table ─────────────────────
                result = _try_condition_criteria_table(t, tdata, row0, pi, pages, page,
                                                       seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Known column heading tables ───────────────────────────
                result = _try_known_kw_table(t, tdata, row0, pi, pages, page,
                                             seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Generic Titled table ─────────────────────────────────────
                result = _try_titled_generic_table(t, tdata, row0, pi, pages, page,
                                                   seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Mechanism of Action appendix table ────────────────────
                result = _try_moa_table(t, tdata, row0, pi, pages, page,
                                        seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue


                # ── Catch-all: render any unclassified table ─────────────
                result = _try_catch_all_table(t, tdata, row0, pi, pages, page,
                                              seen_bboxes, section_boundaries, _t_title)
                if result:
                    nodes.append(result)
                    continue

    return nodes
