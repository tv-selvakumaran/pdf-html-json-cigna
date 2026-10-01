#!/usr/bin/env python3
"""
cigna_build_table_nodes.py
==========================
Table type classification and HTML construction for the Cigna PDF pipeline.

Contains:
  - All _try_*_table handler functions
  - build_table_nodes() dispatcher

Depends on cigna_parse_tables.py for:
  - _section_at, render_generic_table, _is_table_spurious, _true_table_top
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pdfplumber

from cigna_constants import (
    SECTION_VOCAB,
    MM_SECTION_VOCAB,
)

from cigna_parse_nodes import (
    TableNode, TableRowNode,
)

from cigna_parse_headings import classify_section

from cigna_parse_tables import (
    _section_at,
    render_generic_table,
    render_hcpcs_table,
    render_prod_criteria_table_from_labeled,
    render_prod_indications_matrix_table_from_labeled,
    render_pref_criteria_table_from_labeled,
    render_eua_letter_table,
    render_cor_loe_table,
    render_mcd_table,
    render_tte_score_table,
    render_cancer_guidelines_table,
    render_moa_table_from_labeled,
    render_fda_device_mfg_table,
    render_appendix_med_table,
    render_two_col_heading_table,
    _consolidate_pref_criteria_table,
    _is_table_spurious,
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

        html = reconstruct_drug_quantity_table(tdata, gray_bottom=_dql_gray)
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

            # Merge headerless continuation rows into the previous row's
            # matching cell instead of appending as new rows.
            for cont_row in next_tdata:
                _c0 = (cont_row[0] if cont_row else '') or ''
                if not _c0.strip() and combined_rev:
                    # continuation of the last row's cells
                    _prev_row = combined_rev[-1]
                    for ci in range(len(cont_row)):
                        _cont_val = (cont_row[ci] or '').strip()
                        if not _cont_val:
                            continue
                        if ci < len(_prev_row):
                            _prev_val = (_prev_row[ci] or '')
                            _prev_row[ci] = (_prev_val + ' ' + _cont_val).strip() if _prev_val else _cont_val
                        else:
                            # pad row if needed
                            _prev_row.extend([''] * (ci - len(_prev_row) + 1))
                            _prev_row[ci] = _cont_val
                else:
                    combined_rev.extend(next_tdata)

            _p2_bbox_rev = p2tbls[0].bbox
            seen_bboxes.add((_npi_rev,
                round(_p2_bbox_rev[0]), round(_p2_bbox_rev[1]),
                round(_p2_bbox_rev[2]), round(_p2_bbox_rev[3])))
            _npi_rev += 1
        else:
            break

    _cons, _tier_labels = _consolidate_pref_criteria_table(t, combined_rev, page)
    _cons = _join_consolidated_header(_cons, _tier_labels)
    html = reconstruct_revision_table(_cons, None)
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
        # A continuation on the next page is only possible if this is the
        # last real table on its own page. Another table below this one
        # (e.g. the pref_criteria table under a titled matrix) owns any
        # continuation. Height >= 30 mirrors _is_spurious_table's narrow-rule cutoff.
        _t_bbox = tuple(t.bbox)
        _other_table_below = any(
            o.bbox[1] > t.bbox[3] and (o.bbox[3] - o.bbox[1]) >= 30
            for o in page.find_tables()
            if tuple(o.bbox) != _t_bbox)

        # Collect multi-page continuation — must stay within the same section
        _this_section = _section_at(section_boundaries, pi + 1, t.bbox[1])
        _npi2 = pi + 1
        while not _other_table_below and _npi2 < len(pages):
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


def _try_availability_table(tdata, row0, page_num, 
                            title, footnote, top=0.0) -> 'TableNode | None':
    has_product = any((c or '').strip() == 'Product' for c in (row0 or []))
    if has_product:
        return None
    html = reconstruct_availability_table(tdata)
    if html:
        return TableNode(html=html, table_type='availability',
                         page=page_num, top=top, title=title, footnote=footnote)
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
    _p2_table_obj = None
    _p2_page_obj = None
    if pi + 1 < len(pages):
        p2tbls = pages[pi + 1].find_tables()
        if p2tbls:
            p2data = p2tbls[0].extract()
            _p2_table_obj = p2tbls[0]
            _p2_page_obj = pages[pi + 1]
            _p2_bbox_flex = p2tbls[0].bbox
            seen_bboxes.add((pi + 1,
                round(_p2_bbox_flex[0]), round(_p2_bbox_flex[1]),
                round(_p2_bbox_flex[2]), round(_p2_bbox_flex[3])))
    _cons, _tier_labels = _consolidate_pref_criteria_table(t, tdata, page)
    _cons = _join_consolidated_header(_cons, _tier_labels)
    _cons_p2 = None
    if p2data and _p2_table_obj is not None:
        _cons_p2, _tier_labels_p2 = _consolidate_pref_criteria_table(_p2_table_obj, p2data, _p2_page_obj)
        _cons_p2 = _join_consolidated_header(_cons_p2, _tier_labels_p2)
    html = reconstruct_revision_table(_cons, _cons_p2)
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


def build_table_nodes(_pdf: PDF, para_cache: dict = None, 
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
    pages = _pdf.pages
    seen_bboxes: set = set()
    _cor_loe_last_subheading = None
    _cor_loe_last_table_end_page = None

    # ── New workflow: tables of type availability, dql, hcpcs, cpt, icd, appendix_med ─────────────
    # ------------------- prod_criteria, prod_indications_matrix, pref_criteria, ------------
    # ------------------- eua_letter, cor_loe_recommendation ----------------
    _processed_pages: set = set()
    _consumed_entry_ids: set = set()  
    _consumed_bboxes: set = set()
    if table_info:
        _ti_by_page: dict = {}
        for t in table_info:
            _ti_by_page.setdefault(t['page'], []).append(t)

        for entry in table_info:
            if (entry['table_type'] not in ('availability', 
                'dql', 'hcpcs', 'cpt', 'icd', 'appendix_med', 
                'prod_criteria', 'prod_indications_matrix',
                'pref_criteria', 'eua_letter', 'criteria',
                'cor_loe_recommendation', 'revision',
                'medicare_coverage_determination', 'moa',
                'tte_score', 'cancer_guidelines',
                'fda_device_mfg', 'two_col_heading', )):
                continue
            if not entry.get('tdata'):
                continue
            if id(entry) in _consumed_entry_ids:
                continue
            
            pg = entry['page']
            _consumed_bboxes.add(entry['raw_bbox'])
            pi = pg - 1
            page = pages[pi]
            
            # Merge continuation tdata and collect footnote
            tdata = list(entry['tdata'])
            footnote = entry.get('footnote', '')
            _npi = pg
            result = None

            _orig_row0 = [str(c or '').strip() for c in entry['tdata'][0][:2]] \
                         if entry.get('tdata') else []

            while True:
                if entry['table_type'] in ('hcpcs', 'cpt', 'icd', 'tte_score', 'dql', 
                        'revision', 'prod_criteria', 'pref_criteria', 'criteria',
                        'medicare_coverage_determination', 'moa', 'fda_device_mfg',
                        'prod_indications_matrix', 'appendix_med', ):
                    break
                next_entries = _ti_by_page.get(_npi + 1, [])
                next_cont = next(
                    (e for e in next_entries
                                  if e['is_continuation']), None)
                if next_cont and next_cont.get('tdata'):
                    # Stop if continuation row0 doesn't match original row0
                    _cont_row0 = [str(c or '').strip() 
                                  for c in next_cont['tdata'][0][:2]]
                    if _cont_row0 != _orig_row0:
                        break
                    # Stop if NEXT page has a non-continuation table
                    # AFTER the continuation bbox
                    _next_page_entries = _ti_by_page.get(_npi + 1, [])
                    _cont_bbox_bot = next_cont['bbox'][3]
                    _has_new_table = any(
                        not e['is_continuation'] and
                        not str(e['table_type']).startswith('spurious') and
                        e['table_type'] != 'unknown' and
                        e['bbox'][1] > _cont_bbox_bot
                        for e in _next_page_entries)
                    if _has_new_table:
                        # Merge this continuation but then stop
                        tdata = tdata + next_cont['tdata']
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont)) 
                        _consumed_bboxes.add(next_cont['raw_bbox']) 
                        break
                    tdata = tdata + next_cont['tdata']
                    if not footnote and next_cont.get('footnote'):
                        footnote = next_cont['footnote']
                    _processed_pages.add(_npi + 1)
                    _consumed_entry_ids.add(id(next_cont)) 
                    _consumed_bboxes.add(next_cont['raw_bbox']) 
                    _npi += 1
                else:
                    break
            
            row0 = tdata[0] if tdata else []
            
            # Call handler based on table type
            if entry['table_type'] == 'availability':
                result = _try_availability_table(
                    tdata, row0, entry['page'],
                    entry['title'], footnote=footnote,
                    top=entry['bbox'][1])

            elif entry['table_type'] == 'dql':
                _merged_tdata = list(entry['tdata'])
                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'dql_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        _cont_rows = list(next_cont['tdata'])

                        # If the continuation's first row has a populated
                        # column 0 (product name) but empty columns 1+
                        # (strength/quantity), it's a continuation of the
                        # PREVIOUS row's product-name list (a brand-name
                        # synonym carrying over a page break), not a new
                        # row -- merge its text into the most recent row
                        # with a populated column 0 instead of appending
                        # it as its own row. (reconstruct_drug_quantity_
                        # table's own rowspan logic only handles the
                        # OPPOSITE pattern -- empty col0, populated
                        # cols1+ -- so this case needs handling here.)
                        if (_cont_rows and _merged_tdata and _cont_rows[0] and
                                (_cont_rows[0][0] or '').strip() and
                                not any((c or '').strip() for c in _cont_rows[0][1:])):
                            _first_row = _cont_rows.pop(0)
                            _addition = (_first_row[0] or '').strip()
                            for _ridx in range(len(_merged_tdata) - 1, -1, -1):
                                if _merged_tdata[_ridx] and (_merged_tdata[_ridx][0] or '').strip():
                                    _target_row = list(_merged_tdata[_ridx])
                                    _target_row[0] = (
                                        (_target_row[0] or '') + '\n' + _addition
                                    ).strip()
                                    _merged_tdata[_ridx] = _target_row
                                    break

                        _merged_tdata.extend(_cont_rows)
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break

                html = reconstruct_drug_quantity_table(
                    _merged_tdata, gray_bottom=entry.get('gray_bottom'))
                result = TableNode(
                    html=html, table_type='dql',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote) if html else None

            elif entry['table_type'] in ('hcpcs', 'cpt', 'icd'):
                _base_ttype = entry['table_type']
                _cont_ttype = f'{_base_ttype}_continuation'
                _merged_tdata = list(tdata)

                _same_page_entries = [
                    e for e in table_info
                    if e['page'] == pg and
                    e['table_type'] in (_base_ttype, _cont_ttype)
                ]
                _is_last_on_page = (
                    _same_page_entries and
                    max(_same_page_entries, key=lambda e: e['bbox'][1]) is entry
                )

                _npi = pg
                if _is_last_on_page:
                    while True:
                        next_entries = _ti_by_page.get(_npi + 1, [])
                        next_cont = next(
                            (e for e in next_entries
                             if e['table_type'] == _cont_ttype),
                            None)
                        if next_cont and next_cont.get('tdata'):
                            _merged_tdata.extend(next_cont['tdata'])
                            if not footnote and next_cont.get('footnote'):
                                footnote = next_cont['footnote']
                            _processed_pages.add(_npi + 1)
                            _consumed_entry_ids.add(id(next_cont))
                            _consumed_bboxes.add(next_cont['raw_bbox'])
                            _npi += 1
                            # Stop if a FRESH (non-continuation) table of
                            # the same base type also exists on this page,
                            # positioned after the continuation we just
                            # consumed -- that fresh table starts its own,
                            # separate sequence, and this chain must not
                            # bridge across it into whatever continues
                            # THAT table on a later page.
                            _fresh_table_on_this_page = any(
                                e['table_type'] == _base_ttype and
                                e['bbox'][1] > next_cont['bbox'][3]
                                for e in next_entries
                                if e is not next_cont
                            )
                            if _fresh_table_on_this_page:
                                break
                        else:
                            break

                html = render_hcpcs_table(_merged_tdata)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=('header' if _i == 0 else 'data'))
                    for _i, row in enumerate(_merged_tdata)
                ]
                result = TableNode(
                    html=html, table_type=_base_ttype,
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows) if html else None

            elif entry['table_type'] in  ('prod_criteria', 'criteria'):
                # Merge borderless prod_criteria_continuation pages

                # Only the LAST (bottom-most) prod_criteria table on
                # this page should search forward for a continuation --
                # an earlier table on the same page must not claim a
                # continuation that actually belongs to a later table
                # (e.g. Employer Plans above, Individual and Family
                # Plans below, both on page 2 -- the continuation on
                # page 3 belongs to whichever was still open at the
                # bottom of page 2, i.e. the lower one).
                _this_type = entry['table_type']
                _cont_type = f'{_this_type}_continuation'

                _same_page_entries = [
                    e for e in table_info
                    if e['page'] == pg and e['table_type'] == _this_type
                ]
                _is_last_on_page = (
                    _same_page_entries and
                    max(_same_page_entries, key=lambda e: e['bbox'][1]) is entry
                )

                _tier_labels = entry.get('tier_labels') or {}
                _tdata0 = entry['tdata']
                rows_with_labels = [(_tier_labels.get(i, 'data'), row)
                                     for i, row in enumerate(_tdata0)]

                _orig_x0 = entry['bbox'][0]
                _orig_x2 = entry['bbox'][2]

                _npi = pg
                if _is_last_on_page:
                    while True:
                        next_entries = _ti_by_page.get(_npi + 1, [])

                        next_cont = next(
                            (e for e in next_entries
                             if e['table_type'] == _cont_type and
                             id(e) not in _consumed_entry_ids and
                             abs(e['bbox'][0] - _orig_x0) < 5 and
                             abs(e['bbox'][2] - _orig_x2) < 30),
                            None)
                        if next_cont and next_cont.get('tdata'):
                            _cont_tier_labels = next_cont.get('tier_labels') or {}
                            _cont_tdata = next_cont['tdata']
                            for _ri, _row in enumerate(_cont_tdata):
                                _lbl = _cont_tier_labels.get(_ri, 'data')
                                if _lbl == 'header':
                                    continue  # skip repeated header row
                                rows_with_labels.append((_lbl, _row))
                            if not footnote and next_cont.get('footnote'):
                                footnote = next_cont['footnote']
                            _processed_pages.add(_npi + 1)
                            _consumed_entry_ids.add(id(next_cont))
                            _consumed_bboxes.add(next_cont['raw_bbox'])
                            _npi += 1

                            # If this same page ALSO contains a fresh
                            # (non-continuation, non-spurious) table
                            # that is column-aligned with the just-
                            # merged continuation AND separated from it
                            # by a real visual gap (>=30pt), that gap
                            # marks a genuine section boundary. Stop
                            # here rather than absorb the NEXT page's
                            # continuations, which belong to that new
                            # table's own lineage.
                            _fresh_table_on_this_page = False
                            for e in next_entries:
                                if e is next_cont:
                                    continue
                                if (e['table_type'] == _cont_type or
                                        str(e['table_type']).startswith('spurious')):
                                    continue
                                _x0_aligned = abs(e['bbox'][0] - next_cont['bbox'][0]) < 5
                                _x2_aligned = abs(e['bbox'][2] - next_cont['bbox'][2]) < 5
                                _gap = e['bbox'][1] - next_cont['bbox'][3]
                                if _x0_aligned and _x2_aligned and _gap >= 20:
                                    _fresh_table_on_this_page = True
                                    break
                            if _fresh_table_on_this_page:
                                break
                        else:
                            break

                html = render_prod_criteria_table_from_labeled(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type=_this_type,
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'appendix_med':
                _tdata0 = entry['tdata']
                _tier_labels = entry.get('tier_labels') or {}
                rows_with_labels = [(_tier_labels.get(i, 'data'), row)
                                     for i, row in enumerate(_tdata0)]

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['bbox'][1] < 150 and
                         len((e.get('tdata') or [[]])[0] or []) == 2 and
                         not str(e['table_type']).startswith('spurious') and
                         e['table_type'] not in ('revision',
                                                  'hcpcs', 'cpt', 'icd',
                                                  'dql', 'availability')),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        _cont_tier_labels = next_cont.get('tier_labels') or {}
                        for _ri, _row in enumerate(next_cont['tdata']):
                            _lbl = _cont_tier_labels.get(_ri, 'data')
                            if _lbl == 'header':
                                continue
                            rows_with_labels.append((_lbl, _row))
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break

                html = render_appendix_med_table(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='appendix_med',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'prod_indications_matrix':
                _orig_tdata = entry.get('matrix_consolidated_tdata') or entry.get('tdata') or []
                tier_labels = entry.get('matrix_tier_labels') or {}
                labels_in_order = [tier_labels.get(i, 'data') for i in range(len(_orig_tdata))]

                rows_with_labels = list(zip(labels_in_order, _orig_tdata))

                # Only the LAST prod_indications_matrix-family table on
                # this page should search forward for a continuation on
                # the next page -- earlier tables on the same page (e.g.
                # Table 3 before Table 4) must not claim a continuation
                # that actually belongs to a later table, since bbox
                # x0/x2 alone can't disambiguate tables sharing the same
                # column layout.
                _same_page_matrix_entries = [
                    e for e in table_info
                    if e['page'] == pg and
                    e['table_type'] in ('prod_indications_matrix',
                                        'prod_indications_matrix_continuation')
                ]
                _is_last_on_page = (
                    _same_page_matrix_entries and
                    max(_same_page_matrix_entries, key=lambda e: e['bbox'][1]) is entry
                )

                _npi = pg
                if _is_last_on_page:
                    while True:
                        next_entries = _ti_by_page.get(_npi + 1, [])
                        next_cont = next(
                            (e for e in next_entries
                             if e['table_type'] == 'prod_indications_matrix_continuation'),
                            None)
                        if next_cont and next_cont.get('tdata'):
                            for r in next_cont['tdata']:
                                _lbl = 'subgroup' if (
                                    r and sum(1 for c in r if c and str(c).strip()) <= 1
                                ) else 'data'
                                rows_with_labels.append((_lbl, r))
                            if not footnote and next_cont.get('footnote'):
                                footnote = next_cont['footnote']
                            _processed_pages.add(_npi + 1)
                            _consumed_entry_ids.add(id(next_cont))
                            _consumed_bboxes.add(next_cont['raw_bbox'])
                            _npi += 1
                            _fresh_table_on_this_page = any(
                                e['table_type'] == 'prod_indications_matrix' and
                                e['bbox'][1] > next_cont['bbox'][3]
                                for e in next_entries
                                if e is not next_cont
                            )
                            if _fresh_table_on_this_page:
                                break
                        else:
                            break
                html = render_prod_indications_matrix_table_from_labeled(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='prod_indications_matrix',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote, 
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'pref_criteria':
                _tier_labels = entry.get('tier_labels') or {}
                labels_in_order = [_tier_labels[i] for i in sorted(_tier_labels)]
                _orig_tdata = entry.get('tdata') or []
                if len(labels_in_order) != len(_orig_tdata):
                    labels_in_order = ['header'] + ['data'] * (len(_orig_tdata) - 1)

                rows_with_labels = list(zip(labels_in_order, _orig_tdata))

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'pref_criteria_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        _cont_tdata = next_cont.get('consolidated_tdata') or next_cont['tdata']
                        _cont_tier_labels = next_cont.get('tier_labels') or {}
                        for _ri, r in enumerate(_cont_tdata):
                            _cont_lbl = _cont_tier_labels.get(_ri)
                            _nonempty = [c for c in (r or []) if c and str(c).strip()]

                            if _cont_lbl == 'header':
                                _header_like_text = ' '.join(str(c or '') for c in r).lower()
                                _is_repeated_table_header = any(
                                    kw in _header_like_text
                                    for kw in ('non-preferred', 'exception criteria', 'product')
                                )
                                if _is_repeated_table_header:
                                    continue  # genuine repeated header row, skip
                                elif len(_nonempty) == 1:
                                    # Single-cell divider row that happens to
                                    # land in the header shade band (shade
                                    # variance across subgroup banners in
                                    # this PDF) -- treat as subheader.
                                    rows_with_labels.append(('subheader', r))
                                else:
                                    continue  # repeated header row, skip
                            elif _cont_lbl == 'subheader':
                                rows_with_labels.append(('subheader', r))
                            elif (len(_nonempty) <= 1 and rows_with_labels and
                                  rows_with_labels[-1][0] == 'data'):
                                # Genuinely single-cell overflow (e.g. the
                                # whole page is one mega-row of wrapped
                                # text with no tiering of its own) --
                                # append to the previous data row.
                                _prev_lbl, _prev_row = rows_with_labels[-1]
                                _prev_row = list(_prev_row) + [None] * (2 - len(_prev_row))
                                _addition = _nonempty[0] if _nonempty else ''
                                _prev_row[1] = (
                                    (_prev_row[1] or '') + '\n' + _addition
                                ).strip()
                                rows_with_labels[-1] = (_prev_lbl, _prev_row)
                            else:
                                rows_with_labels.append(('data', r))
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break
                html = render_pref_criteria_table_from_labeled(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='pref_criteria',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'eua_letter':
                html = render_eua_letter_table(entry.get('tdata') or [])
                result = TableNode(
                    html=html, table_type='eua_letter',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=entry.get('footnote', '')) if html else None

            elif entry['table_type'] == 'cor_loe_recommendation':
                _tier_labels = entry.get('tier_labels') or {}
                _orig_tdata = entry.get('tdata') or []
                rows_with_labels = []
                _labels_in_order = ([_tier_labels[i] for i in sorted(_tier_labels)]
                                     if _tier_labels and len(_tier_labels) == len(_orig_tdata)
                                     else ['data'] * len(_orig_tdata))
                for lbl, row in zip(_labels_in_order, _orig_tdata):
                    rows_with_labels.append((lbl, row))

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'cor_loe_recommendation_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        for r in next_cont['tdata']:
                            _nonempty = [c for c in (r or []) if c and str(c).strip()]
                            _lbl = 'subheader' if len(_nonempty) <= 1 else 'data'
                            rows_with_labels.append((_lbl, r))
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break

                # Determine whether this table's own rows start with a
                # real divider or straight into data -- if the latter,
                # carry forward whatever subgroup heading was last
                # active from the PREVIOUS cor_loe_recommendation table
                # rendered, so its data rows are correctly attributed.
                _first_rows_have_divider = any(
                    lbl in ('header', 'subheader') and
                    len([c for c in (r or []) if c and str(c).strip()]) <= 1
                    for lbl, r in rows_with_labels[:6]
                )
                _is_adjacent = (_cor_loe_last_table_end_page is not None and
                                 pg == _cor_loe_last_table_end_page + 1)
                _carry_in = None
                if _is_adjacent and not _first_rows_have_divider:
                    _carry_in = _cor_loe_last_subheading

                html, _last_sub = render_cor_loe_table(
                    [row for _, row in rows_with_labels],
                    {i: lbl for i, (lbl, _) in enumerate(rows_with_labels)},
                    carry_in_subheading=_carry_in)
                _cor_loe_last_subheading = _last_sub
                _cor_loe_last_table_end_page = _npi

                result = TableNode(
                    html=html, table_type='cor_loe_recommendation',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote) if html else None

            elif entry['table_type'] == 'medicare_coverage_determination':
                _tier_labels = entry.get('tier_labels') or {}
                _orig_tdata = entry.get('tdata') or []
                labels_in_order = ([_tier_labels[i] for i in sorted(_tier_labels)]
                                    if _tier_labels and len(_tier_labels) == len(_orig_tdata)
                                    else ['data'] * len(_orig_tdata))
                rows_with_labels = list(zip(labels_in_order, _orig_tdata))

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'medicare_coverage_determination_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        _cont_tier_labels = next_cont.get('tier_labels') or {}
                        _cont_labels = ([_cont_tier_labels[i] for i in sorted(_cont_tier_labels)]
                                        if _cont_tier_labels and len(_cont_tier_labels) == len(next_cont['tdata'])
                                        else ['data'] * len(next_cont['tdata']))
                        for lbl, r in zip(_cont_labels, next_cont['tdata']):
                            if lbl == 'header':
                                continue  # repeated header row, skip
                            rows_with_labels.append(('data', r))
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break

                _merged_tdata = [row for _, row in rows_with_labels]
                _merged_tier_labels = {i: lbl for i, (lbl, _) in enumerate(rows_with_labels)}
                html = render_mcd_table(_merged_tdata, _merged_tier_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='medicare_coverage_determination',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote, 
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'tte_score':
                _tier_labels = entry.get('tte_tier_labels') or {}
                _orig_tdata = entry.get('tte_consolidated_tdata') or entry.get('tdata') or []
                labels_in_order = ([_tier_labels[i] for i in sorted(_tier_labels)]
                                    if _tier_labels and len(_tier_labels) == len(_orig_tdata)
                                    else ['data'] * len(_orig_tdata))
                rows_with_labels = list(zip(labels_in_order, _orig_tdata))

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'tte_score_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        # Stop the lineage if a FRESH tte_score table
                        # exists on the same page as this continuation,
                        # positioned after it -- it breaks the chain,
                        # even though a LATER page might have another
                        # tte_score_continuation entry.
                        _cont_bbox_bot = next_cont['bbox'][3]
                        _has_fresh_table_after = any(
                            e['table_type'] == 'tte_score' and
                            e['bbox'][1] > _cont_bbox_bot
                            for e in next_entries)

                        for r in next_cont['tdata']:
                            _nonempty_vals = [c for c in (r or []) if c and str(c).strip()]
                            _lbl = 'subheader' if len(_nonempty_vals) <= 1 else 'data'
                            rows_with_labels.append((_lbl, _nonempty_vals))
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1

                        if _has_fresh_table_after:
                            break
                    else:
                        break

                _merged_tdata = [row for _, row in rows_with_labels]
                _merged_tier_labels = {i: lbl for i, (lbl, _) in enumerate(rows_with_labels)}
                html = render_tte_score_table(_merged_tdata, _merged_tier_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='tte_score',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'cancer_guidelines':
                _tier_labels = entry.get('tier_labels') or {}
                _orig_tdata = entry.get('tdata') or []
                labels_in_order = ([_tier_labels[i] for i in sorted(_tier_labels)]
                                    if _tier_labels and len(_tier_labels) == len(_orig_tdata)
                                    else ['data'] * len(_orig_tdata))
                rows_with_labels = list(zip(labels_in_order, _orig_tdata))

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'cancer_guidelines_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        _cont_tier_labels = next_cont.get('tier_labels') or {}
                        _cont_labels = ([_cont_tier_labels[i] for i in sorted(_cont_tier_labels)]
                                        if _cont_tier_labels and len(_cont_tier_labels) == len(next_cont['tdata'])
                                        else ['data'] * len(next_cont['tdata']))
                        for lbl, r in zip(_cont_labels, next_cont['tdata']):
                            if lbl == 'header':
                                continue  # repeated header row, skip
                            rows_with_labels.append(('data', r))
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break

                _merged_tdata = [row for _, row in rows_with_labels]
                _merged_tier_labels = {i: lbl for i, (lbl, _) in enumerate(rows_with_labels)}
                html = render_cancer_guidelines_table(_merged_tdata, _merged_tier_labels)
                result = TableNode(
                    html=html, table_type='cancer_guidelines',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote) if html else None

            elif entry['table_type'] == 'moa':
                _same_page_moa = [
                    e for e in table_info
                    if e['page'] == pg and e['table_type'] == 'moa'
                ]
                _is_last_on_page = (
                    _same_page_moa and
                    max(_same_page_moa, key=lambda e: e['bbox'][1]) is entry
                )

                _tier_labels = entry.get('tier_labels') or {}
                _tdata0 = entry['tdata']
                _tier_labels = _reclassify_moa_subheaders(_tdata0, _tier_labels)
                rows_with_labels = [(_tier_labels.get(i, 'data'), row)
                                     for i, row in enumerate(_tdata0)]

                _orig_x0 = entry['bbox'][0]
                _orig_x2 = entry['bbox'][2]

                _npi = pg
                if _is_last_on_page:
                    while True:
                        next_entries = _ti_by_page.get(_npi + 1, [])
                        next_cont = next(
                            (e for e in next_entries
                             if e['table_type'] == 'moa_continuation' and
                             id(e) not in _consumed_entry_ids and
                             abs(e['bbox'][0] - _orig_x0) < 5 and
                             abs(e['bbox'][2] - _orig_x2) < 30),
                            None)
                        if next_cont and next_cont.get('tdata'):
                            _cont_tier_labels = next_cont.get('tier_labels') or {}
                            _cont_tdata = next_cont['tdata']
                            _cont_tier_labels = _reclassify_moa_subheaders(_cont_tdata, _cont_tier_labels, is_continuation=True)
                            for _ri, _row in enumerate(_cont_tdata):
                                _lbl = _cont_tier_labels.get(_ri, 'data')
                                if _lbl == 'header':
                                    continue
                                rows_with_labels.append((_lbl, _row))
                            if not footnote and next_cont.get('footnote'):
                                footnote = next_cont['footnote']
                            _processed_pages.add(_npi + 1)
                            _consumed_entry_ids.add(id(next_cont))
                            _consumed_bboxes.add(next_cont['raw_bbox'])
                            _npi += 1

                            _fresh_table_on_this_page = False
                            for e in next_entries:
                                if e is next_cont:
                                    continue
                                if (e['table_type'] == 'moa_continuation' or
                                        str(e['table_type']).startswith('spurious')):
                                    continue
                                _x0_aligned = abs(e['bbox'][0] - next_cont['bbox'][0]) < 5
                                _x2_aligned = abs(e['bbox'][2] - next_cont['bbox'][2]) < 5
                                _gap = e['bbox'][1] - next_cont['bbox'][3]
                                if _x0_aligned and _x2_aligned and _gap >= 20:
                                    _fresh_table_on_this_page = True
                                    break
                            if _fresh_table_on_this_page:
                                break
                        else:
                            break

                html = render_moa_table_from_labeled(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='moa',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'fda_device_mfg':
                _same_page_fda = [
                    e for e in table_info
                    if e['page'] == pg and e['table_type'] == 'fda_device_mfg'
                ]
                _is_last_on_page = (
                    _same_page_fda and
                    max(_same_page_fda, key=lambda e: e['bbox'][1]) is entry
                )

                _tier_labels = entry.get('tier_labels') or {}
                _tdata0 = entry['tdata']
                rows_with_labels = [(_tier_labels.get(i, 'data'), row)
                                     for i, row in enumerate(_tdata0)]

                _orig_x0 = entry['bbox'][0]
                _orig_x2 = entry['bbox'][2]

                _npi = pg
                if _is_last_on_page:
                    while True:
                        next_entries = _ti_by_page.get(_npi + 1, [])
                        next_cont = next(
                            (e for e in next_entries
                             if e['table_type'] == 'fda_device_mfg_continuation' and
                             id(e) not in _consumed_entry_ids and
                             abs(e['bbox'][0] - _orig_x0) < 5 and
                             abs(e['bbox'][2] - _orig_x2) < 30),
                            None)
                        if next_cont and next_cont.get('tdata'):
                            _cont_tier_labels = next_cont.get('tier_labels') or {}
                            _cont_tdata = next_cont['tdata']
                            for _ri, _row in enumerate(_cont_tdata):
                                _lbl = _cont_tier_labels.get(_ri, 'data')
                                if _lbl == 'header':
                                    continue
                                rows_with_labels.append((_lbl, _row))
                            if not footnote and next_cont.get('footnote'):
                                footnote = next_cont['footnote']
                            _processed_pages.add(_npi + 1)
                            _consumed_entry_ids.add(id(next_cont))
                            _consumed_bboxes.add(next_cont['raw_bbox'])
                            _npi += 1

                            _fresh_table_on_this_page = False
                            for e in next_entries:
                                if e is next_cont:
                                    continue
                                if (e['table_type'] == 'fda_device_mfg_continuation' or
                                        str(e['table_type']).startswith('spurious')):
                                    continue
                                _x0_aligned = abs(e['bbox'][0] - next_cont['bbox'][0]) < 5
                                _x2_aligned = abs(e['bbox'][2] - next_cont['bbox'][2]) < 5
                                _gap = e['bbox'][1] - next_cont['bbox'][3]
                                if _x0_aligned and _x2_aligned and _gap >= 20:
                                    _fresh_table_on_this_page = True
                                    break
                            if _fresh_table_on_this_page:
                                break
                        else:
                            break

                html = render_fda_device_mfg_table(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='fda_device_mfg',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'two_col_heading':
                _same_page_tch = [
                    e for e in table_info
                    if e['page'] == pg and e['table_type'] == 'two_col_heading'
                ]
                _is_last_on_page = (
                    _same_page_tch and
                    max(_same_page_tch, key=lambda e: e['bbox'][1]) is entry
                )

                _tier_labels = entry.get('two_col_heading_tier_labels') or {}
                _tdata0 = entry.get('two_col_heading_consolidated_tdata') or entry['tdata']
                rows_with_labels = [(_tier_labels.get(i, 'data'), row)
                                     for i, row in enumerate(_tdata0)]

                _orig_x0 = entry['bbox'][0]
                _orig_x2 = entry['bbox'][2]

                _npi = pg
                if _is_last_on_page:
                    while True:
                        next_entries = _ti_by_page.get(_npi + 1, [])
                        next_cont = next(
                            (e for e in next_entries
                             if e['table_type'] == 'two_col_heading_continuation' and
                             id(e) not in _consumed_entry_ids and
                             abs(e['bbox'][0] - _orig_x0) < 5 and
                             abs(e['bbox'][2] - _orig_x2) < 30),
                            None)
                        if next_cont and next_cont.get('tdata'):
                            _cont_tier_labels = next_cont.get('two_col_heading_tier_labels') or {}
                            _cont_tdata = next_cont.get('two_col_heading_consolidated_tdata') or next_cont['tdata']
                            for _ri, _row in enumerate(_cont_tdata):
                                _lbl = _cont_tier_labels.get(_ri, 'data')
                                if _lbl == 'header':
                                    continue
                                rows_with_labels.append((_lbl, _row))
                            if not footnote and next_cont.get('footnote'):
                                footnote = next_cont['footnote']
                            _processed_pages.add(_npi + 1)
                            _consumed_entry_ids.add(id(next_cont))
                            _consumed_bboxes.add(next_cont['raw_bbox'])
                            _npi += 1

                            _fresh_table_on_this_page = False
                            for e in next_entries:
                                if e is next_cont:
                                    continue
                                if (e['table_type'] == 'two_col_heading_continuation' or
                                        str(e['table_type']).startswith('spurious')):
                                    continue
                                _x0_aligned = abs(e['bbox'][0] - next_cont['bbox'][0]) < 5
                                _x2_aligned = abs(e['bbox'][2] - next_cont['bbox'][2]) < 5
                                _gap = e['bbox'][1] - next_cont['bbox'][3]
                                if _x0_aligned and _x2_aligned and _gap >= 20:
                                    _fresh_table_on_this_page = True
                                    break
                            if _fresh_table_on_this_page:
                                break
                        else:
                            break

                html = render_two_col_heading_table(rows_with_labels)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=lbl)
                    for lbl, row in rows_with_labels
                ]
                result = TableNode(
                    html=html, table_type='two_col_heading',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows,
                    end_page=_npi) if html else None

            elif entry['table_type'] == 'revision':
                _cons = entry.get('consolidated_tdata') or entry['tdata']
                _tier_labels = entry.get('tier_labels') or {}
                _cons = _join_consolidated_header(_cons, _tier_labels)
                _merged_rows = list(_cons)

                _npi = pg
                while True:
                    next_entries = _ti_by_page.get(_npi + 1, [])
                    next_cont = next(
                        (e for e in next_entries
                         if e['table_type'] == 'revision_continuation'),
                        None)
                    if next_cont and next_cont.get('tdata'):
                        _raw_cont_tdata = next_cont['tdata']
                        _tier_labels_cont = next_cont.get('tier_labels') or {}

                        # If this continuation is a single row, don't run
                        # it through column-collapsing at all -- collapsing
                        # requires multiple rows to meaningfully determine
                        # which raw columns are real; for one row, use the
                        # raw tdata directly and match against the already-
                        # established column count from prior pages.
                        if len(_raw_cont_tdata) == 1:
                            _cons_cont = _raw_cont_tdata
                        else:
                            _cons_cont = next_cont.get('consolidated_tdata') or _raw_cont_tdata
                            _cons_cont = _join_consolidated_header(_cons_cont, _tier_labels_cont)
                        _cons_cont = list(_cons_cont)

                        if (_cons_cont and _merged_rows):
                            _first_row = _cons_cont[0]
                            _nonempty_cells = [(i, c) for i, c in enumerate(_first_row) if c and str(c).strip()]
                            if len(_nonempty_cells) == 1:
                                _idx, _c_mid = _nonempty_cells[0]
                                _c_mid = _c_mid.strip()
                                _misdetected_title = (next_cont.get('title') or '').strip()
                                _popped = _cons_cont.pop(0)
                                _last_row = list(_merged_rows[-1])
                                _target_idx = 1 if len(_last_row) > 2 else min(_idx, len(_last_row) - 1)
                                if 0 <= _target_idx < len(_last_row):
                                    _addition = (_misdetected_title + ' ' + _c_mid).strip() if _misdetected_title else _c_mid
                                    _last_row[_target_idx] = (_last_row[_target_idx] + ' ' + _addition).strip()
                                _merged_rows[-1] = tuple(_last_row) if isinstance(_merged_rows[-1], tuple) else _last_row

                        _merged_rows.extend(_cons_cont)
                        if not footnote and next_cont.get('footnote'):
                            footnote = next_cont['footnote']
                        _processed_pages.add(_npi + 1)
                        _consumed_entry_ids.add(id(next_cont))
                        _consumed_bboxes.add(next_cont['raw_bbox'])
                        _npi += 1
                    else:
                        break

                html = reconstruct_revision_table(_merged_rows, None)
                _table_rows = [
                    TableRowNode(cells=row, row_kind=('header' if _i == 0 else 'data'))
                    for _i, row in enumerate(_merged_rows)
                ]
                result = TableNode(
                    html=html, table_type='revision',
                    page=pg, top=entry['bbox'][1],
                    title=entry['title'],
                    footnote=footnote,
                    rows=_table_rows) if html else None

            if result:
                nodes.append(result)
                for p in range(pg, _npi + 1):
                    _processed_pages.add(p)
                _consumed_bboxes.add(entry['bbox'])


    # -------- Old workflow using para_cache, find_tables(), extract_words() and _pdf: PDF---------------------------
    for pi, page in enumerate(pages):
        page_bboxes_build = [t2.bbox for t2 in page.find_tables()]
        for t in page.find_tables():
            _t_bbox_rounded = tuple(round(x, 1) for x in t.bbox)
            if _t_bbox_rounded in _consumed_bboxes:
                continue    # already rendered by new workflow
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

            # Skip tables already classified as spurious by the
            # classification pipeline (table_info), regardless of what
            # this function's own independent re-detection finds --
            # table_info's has_grid_lines-based check is more reliable.
            _matching_entry = next(
                (e for e in table_info
                 if e['page'] == pi + 1 and 
                 all(abs(a-b) < 1.0 for a, b in zip(e['raw_bbox'], t.bbox))), None)
            if _matching_entry and str(_matching_entry.get('table_type', '')).startswith('spurious'):
                continue
            # Skip spurious tables early
            _is_table_spur, _ = _is_table_spurious(tdata, t.bbox, page_bboxes_build,
                                 page=page, page_num=pi+1,
                                 section_boundaries=section_boundaries)
            if _is_table_spur:
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


            # ── FDA-Approved Dosing table ─────────────────────────────────────
            result = _try_fda_dosing_table(t, tdata, row0, pi, pages, page,
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




def _fix_references_year_misclassification(entries: list, section_boundaries, citation_numbers: list) -> tuple[list, dict]:
    """
    In the References section, a numbered entry (etype='num') whose
    value doesn't plausibly continue the citation sequence (e.g. a
    4-digit year like '2025.' matching the same '\\d+[.)]' pattern as
    a real citation number like '1.') is very likely a coincidental
    digit match -- the trailing fragment of the previous citation's
    text wrapped onto its own line, not a genuine new citation marker.
    Merge such a fragment's text directly into the immediately
    preceding entry and drop it from the list, rather than relying on
    downstream continuation logic to re-merge it.
    """
    from reconstruct_cigna_bullet import _clean

    fixed = []
    last_citation_num = None
    for e in entries:
        pg, top, etype, text, x0, size, bold, underline, leading_bold, italic = e
        _in_references = (
            _section_at(section_boundaries, pg, top).lower() == 'references'
            if section_boundaries else False
        )
        if not _in_references:
            last_citation_num = None
        elif etype == 'num':
            _m = re.match(r'^(\d+)[.)]', text) or re.match(r'^(\d+)\s', text)
            if _m:
                _val = int(_m.group(1))
                if last_citation_num is not None and _val > last_citation_num + 3:
                    citation_numbers.pop((pg, top), None)  # this entry is being merged/dropped
                    if fixed:
                        _prev = fixed[-1]
                        _merged_text = _clean(_prev[3] + ' ' + text)
                        fixed[-1] = (_prev[0], _prev[1], _prev[2], _merged_text,
                                     _prev[4], _prev[5], _prev[6], _prev[7],
                                     _prev[8], _prev[9])
                    continue
                else:
                    last_citation_num = _val
        fixed.append(e)
    return fixed, citation_numbers



def _fix_references_citation_numbering(entries: list, section_boundaries) -> tuple[list, dict]:
    """
    In the References section, detect citation markers missing their
    expected punctuation (e.g. '19 Genvoya...' instead of '19. Genvoya...'),
    which causes _is_num's regex ('^\\d+[.)]') to miss them entirely --
    the line falls through as plain continuation text and gets fused
    into the PRECEDING citation instead of starting its own new one.
    Detects a plausible next-citation-number pattern at the start of a
    'plain' entry and promotes it to 'num', forcing a new citation to
    begin there.
    """
    fixed = []
    citation_numbers = {}
    last_citation_num = None
    _prev_top = None
    _prev_pg = None
    _prev_text = None
    for e in entries:
        pg, top, etype, text, x0, size, bold, underline, leading_bold, italic = e
        _in_references = (
            _section_at(section_boundaries, pg, top).lower() == 'references'
            if section_boundaries else False
        )
        if not _in_references:
            last_citation_num = None  # reset once we leave/haven't yet entered References
            _prev_top = None
            _prev_pg = None
            _prev_text = None
        elif etype == 'plain' and x0 <= 75:
            _m = (re.match(r'^(\d+)\s+[A-Z]', text) or
                  re.match(r'^(\d+)\s+[.)]\s+[A-Z]', text))
            if _m:
                _val = int(_m.group(1))
                _is_plausible_first = (last_citation_num is None and _val <= 3)
                _is_plausible_next = (last_citation_num is not None and
                                       1 <= _val - last_citation_num <= 10)
                if _is_plausible_first or _is_plausible_next:
                    etype = 'num'
                    last_citation_num = _val
                    e = (pg, top, etype, text, x0, size, bold, underline, leading_bold, italic)
                    citation_numbers[(pg, top)] = _val
        elif etype == 'num':
            _gap = (top - _prev_top) if (_prev_pg == pg and _prev_top is not None) else 9999
            _prev_line_short = _prev_text is not None and len(_prev_text.strip()) < 60
            _looks_like_real_citation = bool(re.match(r'^\d+[.)]\s+[A-Z][a-z]+', text))
            _fragment_shape = bool(re.match(r'^\d+[.)]\s+[A-Z]{2,}[:.]', text))  # e.g. "9. PMID:"
            _is_wrapped_continuation = (
                _gap < 20.0 and   # smaller than typical between-citation
                                  # spacing (e.g. 12pt within-citation vs
                                  # 24pt between citations, per mm_0089)
                not (_looks_like_real_citation and not _fragment_shape)
            )
            if _is_references_version_number_false_positive(text) or _is_wrapped_continuation:
                # False positive from _is_num's general-purpose regex --
                # this is a wrapped continuation line (e.g. a version
                # number like "2.2025"), not a genuine new citation.
                # Demote back to plain so it merges into the previous
                # citation's text instead of starting a spurious one.
                etype = 'plain'
                e = (pg, top, etype, text, x0, size, bold, underline, leading_bold, italic)
            else:
                _m = re.match(r'^(\d+)[.)]', text)
                if _m:
                    _val = int(_m.group(1))
                    last_citation_num = int(_m.group(1))
                    citation_numbers[(pg, top)] = _val
        fixed.append(e)
        _prev_top = top
        _prev_pg = pg
        _prev_text = text
    return fixed, citation_numbers


def _is_references_version_number_false_positive(text: str) -> bool:
    """
    True if `text` looks like a version-number fragment (e.g.
    '2.2025 - June 27, 2025)...') that _is_num's general-purpose
    regex ('^\\d+[.)]') incorrectly matched as a citation marker --
    a genuine citation marker is always followed by a space and then
    ordinary citation text, never immediately by more digits.
    """
    _m = re.match(r'^(\d+)[.)]', text)
    if not _m:
        return False
    _after_marker = text[_m.end():]
    return bool(_after_marker) and _after_marker[0].isdigit()


def _join_consolidated_header(_cons, _tier_labels):
    """
    Join multiple consolidated header rows (tier_labels=='header') into
    one, column-wise, and return [joined_header_row] + [data_rows...] --
    the flat shape reconstruct_revision_table's fixed-index logic
    already expects.
    """
    if not _cons:
        return _cons
    labels_in_order = ([_tier_labels[i] for i in sorted(_tier_labels)]
                        if _tier_labels and len(_tier_labels) == len(_cons)
                        else ['data'] * len(_cons))
    header_rows = [row for lbl, row in zip(labels_in_order, _cons) if lbl == 'header']
    data_rows = [row for lbl, row in zip(labels_in_order, _cons) if lbl != 'header']

    if not header_rows:
        return _cons

    n_cols = max((len(r) for r in header_rows if r), default=0)
    joined = []
    for ci in range(n_cols):
        parts = [str(r[ci]).strip() for r in header_rows
                 if r and ci < len(r) and r[ci] and str(r[ci]).strip()]
        joined.append(' '.join(parts) if parts else None)

    return [joined] + data_rows



def _reclassify_moa_subheaders(tdata: list, tier_labels: dict, is_continuation: bool = False) -> dict:
    """
    moa-specific tiering correction: a 'header'-tagged row with only
    ONE nonempty cell, appearing after at least one genuine multi-cell
    header row, is a subgroup-heading divider (e.g. 'Biologics',
    'Oral Therapies/Targeted Synthetic Oral Small Molecule Drugs')
    that landed in the same shade band as the table's real header --
    moa tables often have too little shade separation between the
    real header and subgroup dividers to distinguish by threshold
    alone. Reclassify such rows as 'subheader'.

    Returns a NEW tier_labels dict; does not mutate the input.
    """
    _new_labels = dict(tier_labels)
    _header_indices = [i for i, lbl in tier_labels.items() if lbl == 'header']
    _seen_multi_cell_header = False
    for idx in sorted(_header_indices):
        _row = tdata[idx] if idx < len(tdata) else None
        _nonempty_count = len([c for c in (_row or []) if c and str(c).strip()])
        if _nonempty_count > 1:
            _seen_multi_cell_header = True
        elif _nonempty_count == 1 and (is_continuation or _seen_multi_cell_header):
            _new_labels[idx] = 'subheader'
    return _new_labels



def _reclassify_two_col_heading_subheaders(tdata: list, tier_labels: dict) -> dict:
    """
    two_col_heading-specific tiering correction: only the leading,
    contiguous block of 'header'-tagged rows (from index 0, before any
    'data' row appears) is the table's genuine header. Any 'header'-
    tagged row appearing AFTER the first data row is actually a
    subheading (a section divider), not a repeated/extended header --
    two_col_heading tables can have multiple such subheadings
    scattered throughout, distinguished structurally by position, not
    by shade or cell count (which are unreliable here, since a
    section heading can be a single long text block just like the
    genuine header).

    Returns a NEW tier_labels dict; does not mutate the input.
    """
    _new_labels = dict(tier_labels)
    _seen_data = False
    for i in sorted(tier_labels.keys()):
        _lbl = tier_labels[i]
        if _lbl == 'data':
            _seen_data = True
        elif _lbl == 'header' and _seen_data:
            _new_labels[i] = 'subheader'
    return _new_labels
