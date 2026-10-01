#!/usr/bin/env python3
"""
cigna_parse.py  —  V4: PDF → Document Tree
============================================
Pass 1 of the two-pass V5 converter.
"""

from __future__ import annotations
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pdfplumber

from cigna_extractor import (
    extract_raw_lines as _extract_raw_lines_ext,
    extract_paragraph_lines as _extract_para_lines,
    _has_underline_rect, _find_section_boundaries,
)
from reconstruct_cigna_bullet import reconstruct

from cigna_parse_nodes import (
    CignaDoc, HeaderNode, SectionNode, SubsectionNode, SubSubsectionNode,
    ParagraphBlockNode, FootnoteNode, IFUNode, TableNode, FooterNode,
    TOCNode, RelatedResourcesNode, TableRowNode, ReferenceItemNode, ReferencesNode,
    clean as _clean, is_boilerplate as _is_boilerplate,
    is_page_footer as _is_page_footer,
    identify_paragraph_boundaries, 
    _convert_references_section,
    _tag_general_background_zones,
    _link_general_background_citations,
    _build_reference_lookup,

)
from cigna_parse_headings import (classify_section, classify_subsection,
                                   classify_subsubsection, classify_mm_subsection, 
                                   normalize_heading,
)

from cigna_parse_tables import (
    _is_mm_family_by_prefix, 
    _is_cpg_family, _is_ph_family,
    find_table_info, _classify_table_types, 
    _rescue_pref_criteria_continuations, 
    _rescue_hcpcs_cpt_icd_continuations,
    inject_tables_into_tree, 
    _revert_spurious_reasons_for_drug_family,
    _apply_network_adequacy_override,
    _apply_dosing_table_override,
    _apply_three_step_medications_override,
    _apply_toc_table_override,
    _APPROVED_SPURIOUS_REASONS, _SPURIOUS_REASON_TYPES,
)

from cigna_build_table_nodes import build_table_nodes

from cigna_constants import (
    FOOTER_PATTERNS,
    SECTION_VOCAB,
    MM_SECTION_VOCAB,
    CPG_SECTION_VOCAB,
    PH_SECTION_VOCAB,
)


# ════════════════════════════════════════════════════════════════════════════
# Metadata extraction
# ════════════════════════════════════════════════════════════════════════════

def _extract_meta(lines: list[dict], pdf_path: Path) -> dict:
    meta = {
        'title':        '',
        'policy_id':    pdf_path.stem,
        'publish_date': '',
        'policy_type':  'commercial',
        'doc_type':     'Coverage Policy',
        'source_url':   (
            'https://www.cigna.com/static/www-cigna-com/docs/health-care-provider/'
            f'resources/coverage-policies/{pdf_path.stem}.pdf'),
    }
    title_parts = []
    for line in lines:
        if line['page'] > 2:
            break
        text, bold, size = line['text'], line['bold'], line['size']
        if size >= 18.0 and bold:
            title_parts.append(text)
        m = re.search(
            r'Effective\s+Date[.:\s\u2026]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})',
            text, re.I)
        if m:
            meta['publish_date'] = m.group(1).strip()
        m = re.search(r'Coverage\s+Policy\s+Number\s*[.:\s\u2026]+(\S+)', text, re.I)
        if m:
            meta['policy_id'] = m.group(1).strip()
        m = re.search(r'Next\s+Review\s+Date[.:\s\u2026]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})', text, re.I)
        if m:
            meta['next_review_date'] = m.group(1).strip()
        tl = text.lower()
        if bold and ('drug coverage' in tl or 'drug and biologic' in tl):
            meta['policy_type'] = 'drug_policy'
            meta['doc_type']    = text.strip()
        elif bold and 'medical coverage' in tl:
            meta['policy_type'] = 'medical_policy'
            meta['doc_type']    = text.strip()
        elif bold and 'administrative policy' in tl:
            meta['policy_type'] = 'administrative_policy'
            meta['doc_type']    = text.strip()
    if title_parts:
        meta['title'] = _clean(' '.join(title_parts))
    return meta


def _parse_toc_rcr(_pdf: PDF, table_bboxes: set = frozenset()) -> tuple[TOCNode, RelatedResourcesNode]:
    """Extract TOC and Related Coverage Resources from page 1.
    RCR entries are (text, url) tuples extracted from PDF annotations.

    TOC and RCR appear side-by-side in a known two-column layout. A
    dedicated column-aware re-extraction (via extract_raw_lines'
    column_split_x0 parameter) is used for this zone specifically, so
    a TOC line and an RCR line that happen to fall within the same
    y-proximity tolerance are never merged into one text string.
    """
    import re
    toc_entries = []
    rcr_entries = []
    in_toc_zone = False

    from cigna_extractor import _find_ifu_bounds, extract_raw_lines as _extract_raw_lines_split
    ifu_bounds = _find_ifu_bounds(_pdf)
    ifu_start_page = ifu_bounds[0][0] if ifu_bounds else 99
    ifu_start_top  = ifu_bounds[0][1] if ifu_bounds else 9999

    uri_annots = []
    try:
        page = _pdf.pages[0]
        uri_annots = [
            a for a in (page.annots or [])
            if a.get('uri') and a['x0'] > 280
        ]
    except Exception:
        pass

    def _url_for_line(line_top: float) -> str:
        for a in uri_annots:
            if a['top'] <= line_top <= a['bottom']:
                return a['uri']
        return ''

    # Derive the column split dynamically from the RCR hyperlink
    # annotations' actual x0 positions, rather than a fixed threshold --
    # different documents may have slightly different column widths.
    if uri_annots:
        _rcr_x0_min = min(a['x0'] for a in uri_annots)
        # Split partway between the TOC column's typical text start
        # (~54-72) and the RCR column's actual leftmost annotation --
        # comfortably past any TOC page-number digits, safely before
        # RCR's real text.
        _column_split_x0 = max(200, _rcr_x0_min - 20)
    else:
        _column_split_x0 = 280  # fallback if no annotations found
    zone_lines = _extract_raw_lines_split(_pdf, table_bboxes, column_split_x0=_column_split_x0)

    rcr_groups = {}
    rcr_order  = []

    for l in zone_lines:
        text = l['text'].strip()
        top  = l['top']
        x0   = l['x0']

        if l['bold'] and l['size'] >= 12:
            _tl = text.lower()
            if 'table of contents' in _tl:
                in_toc_zone = True
                continue
            if 'related coverage resources' in _tl:
                # RCR-side fragment of the same combined heading line,
                # appearing before in_toc_zone is set by the TOC-side
                # fragment (processed at a slightly different top/order)
                continue

        if not in_toc_zone:
            continue

        if (l['page'] > ifu_start_page or
                (l['page'] == ifu_start_page and l['top'] >= ifu_start_top)):
            break

        if x0 < 280:
            clean = re.sub(r'[\.\u2025\u2026\s]+\d+\s*$', '', text).strip()
            clean = re.sub(r'\s+\d+\s*$', '', clean).strip()
            if clean:
                m = re.search(r'(\d+)\s*$', text)
                pg = m.group(1) if m else ''
                toc_entries.append((clean, pg))
        else:
            url = _url_for_line(top)
            if url not in rcr_groups:
                rcr_groups[url] = []
                rcr_order.append(url)
            rcr_groups[url].append(text)

    for url in rcr_order:
        text = ' '.join(rcr_groups[url])
        rcr_entries.append((text, url))

    return (TOCNode(entries=toc_entries),
            RelatedResourcesNode(entries=rcr_entries))


def _promote_orphan_subsubsections(doc: CignaDoc) -> None:
    """
    Post-processing pass: promote SubSubsectionNodes that are direct
    children of SectionNodes to SubsectionNodes.
    """
    for node in doc.nodes:
        if not isinstance(node, SectionNode):
            continue
        new_children = []
        for child in node.children:
            if isinstance(child, SubSubsectionNode):
                # Promote to SubsectionNode
                promoted = SubsectionNode(
                    heading=child.heading,
                    page=child.page,
                    top=child.top)
                promoted.children = getattr(child, 'children', [])
                new_children.append(promoted)
            else:
                new_children.append(child)
        node.children = new_children


# ════════════════════════════════════════════════════════════════════════════
# Table boundaries 
# ════════════════════════════════════════════════════════════════════════════

def _in_table_boundary(pg: int, top: float, table_bboxes) -> bool:
    """
    True if (pg, top) falls inside any table's (enlarged) boundary span.
    table_bboxes entries are 6-tuples: (start_page, start_top, enlarged_start_top,
    end_page, end_bottom, enlarged_end_bottom) -- same convention as
    covered_bboxes in find_table_info / extract_raw_lines' in_table check.
    """
    return any(
        sp <= pg <= ep and en_st <= top <= en_eb
        for sp, st, en_st, ep, eb, en_eb in table_bboxes
    )


# ════════════════════════════════════════════════════════════════════════════
# Main parser
# ════════════════════════════════════════════════════════════════════════════

def parse(pdf_path: Path) -> CignaDoc:
    """Full Pass 1: PDF → CignaDoc tree."""
    pdf_path = Path(pdf_path)

    with pdfplumber.open(str(pdf_path)) as _pdf:
        _page_count = len(_pdf.pages)
        # ── Step 1: Pre-extract paragraph lines for every page ────────────────
        _para_cache: dict[int, list[dict]] = {}
        for _pg in range(1, _page_count + 1):
            _lines = _extract_para_lines(_pdf, _pg)
            for _l in _lines:
                _l['_page'] = _pg
            _para_cache[_pg] = [
                _l for _l in _lines
                if not any(p.match(_l['text'].strip()) 
                           for p in FOOTER_PATTERNS)
            ]

        # ── Step 2: Table detection (uses _para_cache for title lookup) ───────
        table_info, table_bboxes, _spurious_bboxes = find_table_info(_pdf, para_cache=_para_cache)
        _table_bboxes_out = table_bboxes  # saved for return value
        raw_lines  = _extract_raw_lines_ext(_pdf, table_bboxes)
        meta       = _extract_meta(raw_lines, pdf_path)

        _prefix_result = _is_mm_family_by_prefix(pdf_path.stem)
        _is_cpg = False
        if _prefix_result is not None:
            _is_mm_family = _prefix_result
        else:
            # Unknown prefix -- fall back to bold-text policy_type detection
            _is_mm_family = meta.get('policy_type') in ('medical_policy', 'administrative_policy')
        if _is_mm_family and _is_cpg_family(pdf_path.stem):
            _section_vocab = CPG_SECTION_VOCAB
            _is_cpg = True
        elif _is_mm_family:
            _section_vocab = MM_SECTION_VOCAB
        elif _is_ph_family(pdf_path.stem):
            _section_vocab = PH_SECTION_VOCAB
        else:
            _section_vocab = SECTION_VOCAB

        # Precompute underline flags for mm_ family
        if _is_mm_family:
            for _pg in range(1, _page_count + 1):
                _page = _pdf.pages[_pg - 1]
                for _l in _para_cache.get(_pg, []):
                    _l['underline'] = _has_underline_rect(_page, _l)
                for _l in raw_lines:
                    if _l['page'] == _pg:
                        _l['underline'] = _has_underline_rect(_page, _l)

        # Parse TOC and Related Coverage Resources if present
        _toc_node, _rcr_node = _parse_toc_rcr(_pdf, table_bboxes)
        _has_toc = bool(_toc_node.entries)

        _section_boundaries = _find_section_boundaries(raw_lines, _section_vocab, _pdf, is_cpg=_is_cpg, para_cache=_para_cache)
        _classify_table_types(table_info, _section_boundaries,
                               table_bboxes=table_bboxes,
                               spurious_narrow_rule_bboxes=_spurious_bboxes,
                               policy_id=pdf_path.stem,
                               raw_lines=raw_lines)
        _rescue_pref_criteria_continuations(table_info, _section_boundaries)
        _rescue_hcpcs_cpt_icd_continuations(table_info, _section_boundaries, table_bboxes)
        _apply_network_adequacy_override(table_info, _section_boundaries, pdf_path.stem)
        _apply_toc_table_override(table_info, _section_boundaries, pdf_path.stem)
        _apply_three_step_medications_override(table_info)
        _apply_dosing_table_override(table_info)

        if not _is_mm_family:
            _revert_spurious_reasons_for_drug_family(table_info)

        table_nodes = build_table_nodes(_pdf, para_cache=_para_cache, table_info=table_info,
                                        section_boundaries=_section_boundaries, 
                                        section_vocab=_section_vocab)

        # Build a lookup: (page, bbox_top_rounded) -> title
        _title_lookup = {}
        for entry in table_info:
            key = (entry['page'], round(entry['bbox'][1]))
            _title_lookup[key] = entry['title']

        # Backfill titles onto TableNodes
        for tn in table_nodes:
            if not tn.title:
                key = (tn.page, round(tn.top))
                tn.title = _title_lookup.get(key, '')

        # Build a lookup of table bottoms by page for footnote detection
        _table_bottoms: dict[int, list[float]] = defaultdict(list)
        for (sp, st, en_st, ep, eb, en_eb) in _table_bboxes_out:
            _table_bottoms[ep].append(en_eb)

        doc    = CignaDoc(meta=meta)
        header = HeaderNode(meta=meta)
        doc.nodes.append(header)

        if _has_toc:
            doc.nodes.append(_toc_node)
        if _rcr_node.entries:
            doc.nodes.append(_rcr_node)

        tables_by_type: dict[str, list[TableNode]] = defaultdict(list)
        for tn in table_nodes:
            tables_by_type[tn.table_type].append(tn)

        # ── Parser state ─────────────────────────────────────────────────────
        current_section : Optional[SectionNode]    = None
        current_sub     : Optional[SubsectionNode] = None
        _sub_fragment   : Optional[str] = None
        _sub_frag_y     : float         = 0.0

        para_lines : list  = []
        last_y     : float = 0.0
        last_page  : int   = 0

        def _target() -> list:
            if current_sub is not None:
                return current_sub.children
            if current_section is not None:
                return current_section.children
            return doc.nodes

        def _flush_para() -> None:
            nonlocal para_lines
            if not para_lines:
                return
            ordered = sorted(para_lines, key=lambda l: (l.get('_page', 0), l['top']))
            block = reconstruct(ordered)
            if block.plain_text or block.items:
                _target().append(ParagraphBlockNode(block=block))
            para_lines = []

        def _close_sub() -> None:
            nonlocal current_sub
            if current_sub is not None and current_section is not None:
                current_section.children.append(current_sub)
                current_sub = None

        def _close_section() -> None:
            nonlocal current_section
            _close_sub()
            if current_section is not None:
                doc.nodes.append(current_section)
                current_section = None

        def _accumulate(page: int, top: float) -> None:
            cached = _para_cache.get(page, [])
            matching = [l for l in cached
                        if abs(l['top'] - top) <= 2
                        and not classify_section(l, vocab=_section_vocab)]
            para_lines.extend(matching if matching else [])


        # ── Helper: which segment does this line belong to? ──────────────────
        def _segment_for_line(line: dict) -> str:
            seg = 'header'
            for b in _section_boundaries:
                if (b['page'] < line['page'] or
                        (b['page'] == line['page'] and
                         b['top'] <= line['top'])):
                    seg = b['segment']
                else:
                    break
            return seg

        # ── Track current segment for section transitions ────────────────────
        _current_seg: str = 'header'
        ifu_texts: list[str] = []

        # Pre-compute doc entries
        from reconstruct_cigna_bullet import _resolve_entries, _build_block
        _doc_entries: dict[int, list] = {}
        for _pg in range(1, _page_count + 1):
            _lines_for_pg = [
                l for l in _para_cache.get(_pg, [])
                if not _in_table_boundary(_pg, l['top'], table_bboxes)
            ]
            _doc_entries[_pg] = _resolve_entries(_lines_for_pg, is_cpg=_is_cpg)

        # Fix References-section citation numbers being confused with
        # embedded years (e.g. '2025.' matching the same numbered-item
        # pattern as '1.') -- merge such fragments back into the
        # preceding citation's text rather than treating them as new
        # numbered items.
        from cigna_parse_tables import _section_at
        from cigna_build_table_nodes import (
                _fix_references_year_misclassification, _fix_references_citation_numbering,
                )
        _flat_entries = []
        for _pg in range(1, _page_count + 1):
            _flat_entries.extend(_doc_entries[_pg])
        _flat_entries.sort(key=lambda e: (e[0], e[1]))

        _flat_entries, _citation_numbers = _fix_references_citation_numbering(
            _flat_entries, _section_boundaries)

        _flat_entries, citation_numbers = _fix_references_year_misclassification(
            _flat_entries, _section_boundaries, _citation_numbers)

        _doc_entries = {}
        for e in _flat_entries:
            _doc_entries.setdefault(e[0], []).append(e)

        # ── Outer loop: segments ──────────────────────────────────────────
        for i, boundary in enumerate(_section_boundaries):
            seg = boundary['segment']
            seg_start = (boundary['page'], boundary['top'])
            seg_end = (_section_boundaries[i+1]['page'],
                       _section_boundaries[i+1]['top']) \
                      if i+1 < len(_section_boundaries) \
                      else (_page_count+1, 0.0)

            # ── Special segments ──────────────────────────────────────────
            if seg == 'header':
                continue

            elif seg == 'ifu':
                # Collect IFU text from raw_lines in this segment
                ifu_texts = [
                    l['text'] for l in raw_lines
                    if (l['page'] > seg_start[0] or
                        (l['page'] == seg_start[0] and
                         l['top'] > seg_start[1])) and
                       (l['page'] < seg_end[0] or
                        (l['page'] == seg_end[0] and
                         l['top'] < seg_end[1])) and
                    l['text'].strip().lower() not in
                        ('instructions for use', 'purpose')
                ]
                _ifu_heading = 'Purpose' if _is_mm_family \
                               else 'Instructions for Use'
                doc.nodes.append(IFUNode(
                    text=_clean(' '.join(ifu_texts)),
                    heading=_ifu_heading))

            elif seg == 'footer':
                footer_lines = [
                    l['text'] for l in raw_lines
                    if (l['page'] > seg_start[0] or
                        (l['page'] == seg_start[0] and
                         l['top'] > seg_start[1])) and
                       (l['page'] < seg_end[0] or
                        (l['page'] == seg_end[0] and
                         l['top'] < seg_end[1]))
                ]
                doc.nodes.append(FooterNode(
                    text=' '.join(footer_lines)))

            else:
                _close_section()
                _heading = 'Applicable Products' if seg == 'applicable_products' else seg
                _seg_start = (seg_start[0], seg_start[1] - 1.0) \
                             if seg == 'applicable_products' else seg_start
                _include_start = True if seg == 'applicable_products' else False
                current_section = SectionNode(heading=_heading,
                                              page=seg_start[0],
                                              top=seg_start[1])

                seg_entries = []
                for _pg in range(seg_start[0], seg_end[0] + 1):
                    seg_entries.extend(_doc_entries.get(_pg, []))

                items = identify_paragraph_boundaries(
                    seg_entries, seg_start, seg_end,
                    table_bboxes, gap_threshold = 18.0, 
                    is_mm_family = _is_mm_family,
                    is_cpg_family=_is_cpg,
                    current_section = _heading.lower(), 
                    include_start = _include_start )

                # ── Middle loop: paragraphs ───────────────────────────────────
                for item in items:
                    if item['type'] == 'subsection':
                        if item['level'] == 1:
                            _close_sub()
                            current_sub = SubsectionNode(
                                heading=item['heading'],
                                page=item['page'],
                                top=item['top'])
                        elif item['level'] == 2:
                            _close_sub()
                            _node = SubSubsectionNode(
                                heading=item['heading'],
                                page=item['page'],
                                top=item['top'])
                            if current_sub is not None:
                                current_sub.children.append(_node)
                            else:
                                current_section.children.append(_node)

                    elif item['type'] == 'paragraph':
                        para_entries = [
                            e for e in seg_entries
                            if (e[0] > item['start_page'] or
                                (e[0] == item['start_page'] and
                                 e[1] >= item['start_top'])) and
                               (e[0] < item['end_page'] or
                                (e[0] == item['end_page'] and
                                 e[1] <= item['end_bottom']))
                        ]
                        if not para_entries:
                            continue
                        block = _build_block(para_entries, citation_numbers = _citation_numbers)

                        if block.plain_text or block.items:
                            _target().append(ParagraphBlockNode(
                                block=block,
                                page=item['start_page'],
                                top=item['start_top'],
                                end_page=item['end_page'],
                                end_bottom=item['end_bottom']))

                _close_sub()
            _close_section()

        # ── Post-processing ───────────────────────────────────────────────
        _promote_orphan_subsubsections(doc)
        _convert_references_section(doc)
        _tag_general_background_zones(doc, _pdf)
        _reference_lookup = _build_reference_lookup(doc)
        _link_general_background_citations(doc, _reference_lookup)
        inject_tables_into_tree(doc, tables_by_type)

    return doc, _table_bboxes_out


# ════════════════════════════════════════════════════════════════════════════
# CLI: print tree
# ════════════════════════════════════════════════════════════════════════════

def _print_tree(doc: CignaDoc) -> None:
    from reconstruct_cigna_bullet import (
        BulletItem, SubBulletItem, SubSubItem,
        NumItem, LetterItem, RomanItem, NoteItem, PlainText)

    print(f"\nDOCUMENT: {doc.meta.get('policy_id')}  —  {doc.meta.get('title', '')}")
    print(f"  doc_type:  {doc.meta.get('doc_type')}")
    print(f"  pub_date:  {doc.meta.get('publish_date')}")

    def _show(nodes, indent=0):
        pad = '  ' * indent
        for n in nodes:
            if isinstance(n, HeaderNode):
                print(f"{pad}[HEADER]  bullets={len(n.product_bullets)}")
            elif isinstance(n, IFUNode):
                print(f"{pad}[IFU]  {n.text[:80]}")
            elif isinstance(n, SectionNode):
                print(f"{pad}[SECTION]  {n.heading}")
                _show(n.children, indent + 1)
            elif isinstance(n, SubsectionNode):
                print(f"{pad}[SUBSECTION]  {n.heading}")
                _show(n.children, indent + 1)
            elif isinstance(n, ParagraphBlockNode):
                b = n.block
                if b.plain_text:
                    print(f"{pad}[PARA]  {b.plain_text[:100]}")
                for item in b.items:
                    if isinstance(item, BulletItem):
                        print(f"{pad}[BULLET]  • {item.text[:100]}")
                        for sub in item.children:
                            print(f"{pad}  [SUB]  ○ {sub.text[:100]}")
                            for ssub in sub.children:
                                print(f"{pad}    [SUBSUB]  ▪ {ssub.text[:100]}")
                    elif isinstance(item, NumItem):
                        print(f"{pad}[NUM]  {item.text[:100]}")
                        for ni in getattr(item, 'notes', []):
                            print(f"{pad}  [NOTE]  {ni.text[:100]}")
                        for li in item.children:
                            print(f"{pad}  [LETTER]  {li.text[:100]}")
                            for ri in getattr(li, 'children', []):
                                print(f"{pad}    [ROMAN]  {ri.text[:100]}")
                    elif isinstance(item, NoteItem):
                        print(f"{pad}[NOTE]  {item.text[:100]}")
                    elif isinstance(item, PlainText):
                        print(f"{pad}[PARA]  {item.text[:100]}")
            elif isinstance(n, TableNode):
                print(f"{pad}[TABLE:{n.table_type}]")
            elif isinstance(n, FootnoteNode):
                print(f"{pad}[FOOTNOTE]  {n.text[:100]}")

    _show(doc.nodes)


if __name__ == '__main__':
    import argparse, sys as _sys, io
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf', required=True)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    doc, _ = parse(Path(args.pdf).expanduser())
    if args.out:
        buf = io.StringIO()
        old = _sys.stdout; _sys.stdout = buf
        _print_tree(doc)
        _sys.stdout = old
        Path(args.out).write_text(buf.getvalue(), encoding='utf-8')
        print(f"Tree written to: {args.out}", file=_sys.stderr)
    else:
        _print_tree(doc)
