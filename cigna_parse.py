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

from extractor import (
    extract_raw_lines as _extract_raw_lines_ext,
    extract_paragraph_lines as _extract_para_lines,
    _has_underline_rect, _find_section_boundaries,
)
from reconstruct_cigna_bullet import reconstruct

from cigna_parse_nodes import (
    CignaDoc, HeaderNode, SectionNode, SubsectionNode, SubSubsectionNode,
    ParagraphBlockNode, FootnoteNode, IFUNode, TableNode, FooterNode,
    TOCNode, RelatedResourcesNode,
    clean as _clean, is_boilerplate as _is_boilerplate,
    is_footer as _is_footer,
)
from cigna_parse_headings import (classify_section, classify_subsection,
                                   classify_subsubsection, classify_mm_subsection, 
                                   normalize_heading)

from cigna_parse_nodes import (SECTION_VOCAB, MM_SECTION_VOCAB)

from cigna_parse_tables import (
    find_table_info, inject_tables_into_tree,
)
from cigna_build_table_nodes import build_table_nodes


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
            r'Effective\s+Date[.\s\u2026]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})',
            text, re.I)
        if m:
            meta['publish_date'] = m.group(1).strip()
        m = re.search(r'Coverage\s+Policy\s+Number\s*[.\s]+(\S+)', text, re.I)
        if m:
            meta['policy_id'] = m.group(1).strip()
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


def _parse_toc_rcr(raw_lines: list, pdf_path: Path) -> tuple[TOCNode, RelatedResourcesNode]:
    """Extract TOC and Related Coverage Resources from page 1.
    RCR entries are (text, url) tuples extracted from PDF annotations.
    """
    import re, pdfplumber
    toc_entries = []
    rcr_entries = []  # list of (text, url)
    in_toc_zone = False

    # ── Extract URI annotations from page 1 (right column) ───────────
    uri_annots = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            page = pdf.pages[0]
            uri_annots = [
                a for a in (page.annots or [])
                if a.get('uri') and a['x0'] > 200
            ]
    except Exception:
        pass

    def _url_for_line(line_top: float) -> str:
        """Find URL for a raw_line based on its top coordinate."""
        for a in uri_annots:
            if a['top'] <= line_top <= a['bottom']:
                return a['uri']
        return ''

    # ── Parse TOC and RCR text lines ─────────────────────────────────
    # Group RCR lines by URL
    rcr_groups = {}   # url -> [text fragments]
    rcr_order  = []   # ordered list of urls (preserves order)

    for l in raw_lines:
        if l['page'] > 1:
            break
        text = l['text'].strip()
        top  = l['top']
        x0   = l['x0']

        # Detect TOC/RCR heading line
        if ('table of contents' in text.lower() and
                l['bold'] and l['size'] >= 12):
            in_toc_zone = True
            continue

        if not in_toc_zone:
            continue

        # End of zone
        if 'instructions for use' in text.lower() and l['bold']:
            break

        if x0 < 200:
            # LEFT: TOC entry
            clean = re.sub(r'[\.\u2025\u2026\s]+\d+\s*$', '', text).strip()
            clean = re.sub(r'\s+\d+\s*$', '', clean).strip()
            if clean:
                m = re.search(r'(\d+)\s*$', text)
                pg = m.group(1) if m else ''
                toc_entries.append((clean, pg))
        else:
            # RIGHT: RCR entry — group by URL
            url = _url_for_line(top)
            if url not in rcr_groups:
                rcr_groups[url] = []
                rcr_order.append(url)
            rcr_groups[url].append(text)

    # Assemble RCR entries as (text, url) tuples
    for url in rcr_order:
        text = ' '.join(rcr_groups[url])
        rcr_entries.append((text, url))

    return (TOCNode(entries=toc_entries),
            RelatedResourcesNode(entries=rcr_entries))



# ════════════════════════════════════════════════════════════════════════════
# Main parser
# ════════════════════════════════════════════════════════════════════════════

def parse(pdf_path: Path) -> CignaDoc:
    """Full Pass 1: PDF → CignaDoc tree."""
    pdf_path = Path(pdf_path)

    # ── Step 1: Pre-extract paragraph lines for every page ────────────────
    # This runs first so _para_cache is available for title lookup in
    # find_table_info (table titles are bold lines just above table bboxes).
    with pdfplumber.open(str(pdf_path)) as _pdf:
        _page_count = len(_pdf.pages)
    _para_cache: dict[int, list[dict]] = {}
    for _pg in range(1, _page_count + 1):
        _lines = _extract_para_lines(pdf_path, _pg)
        for _l in _lines:
            _l['_page'] = _pg
        _para_cache[_pg] = _lines

    # ── Step 2: Table detection (uses _para_cache for title lookup) ───────
    table_info, table_bboxes = find_table_info(pdf_path, para_cache=_para_cache)
    _table_bboxes_out = table_bboxes  # saved for return value
    raw_lines  = _extract_raw_lines_ext(pdf_path, table_bboxes)
    meta       = _extract_meta(raw_lines, pdf_path)

    # Detect document family
    _is_mm_family = meta.get('policy_type') in (
        'medical_policy', 'administrative_policy'
    )
    # Fallback: filename prefix, only used if doc_type extraction was empty/ambiguous
    if not _is_mm_family and not meta.get('doc_type'):
        # fall back to filename prefix for en_/hm_/um_ families
        _prefix = pdf_path.stem.split('_')[0].lower()
        _is_mm_family = _prefix in ('mm', 'ad', 'en', 'hm') or pdf_path.stem.startswith('um')

    _section_vocab = MM_SECTION_VOCAB if _is_mm_family else SECTION_VOCAB

    # Precompute underline flags for mm_ family
    if _is_mm_family:
        with pdfplumber.open(str(pdf_path)) as _pdf:
            for _pg in range(1, _page_count + 1):
                _page = _pdf.pages[_pg - 1]
                for _l in _para_cache.get(_pg, []):
                    _l['underline'] = _has_underline_rect(_page, _l)

    # Parse TOC and Related Coverage Resources if present
    _toc_node, _rcr_node = _parse_toc_rcr(raw_lines, pdf_path)
    _has_toc = bool(_toc_node.entries)

    _section_boundaries = _find_section_boundaries(
        raw_lines, _section_vocab, pdf_path)
    table_nodes = build_table_nodes(pdf_path, para_cache=_para_cache, 
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
    for (pg, top, bot) in _table_bboxes_out:
        _table_bottoms[pg].append(bot)

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

    # ── Main line loop ───────────────────────────────────────────────────
    for line in raw_lines:
        text = line['text']
        top  = line['top']
        page = line['page']

        if not text:
            continue
        if _is_footer(top, text):
            continue

        seg = _segment_for_line(line)

        # ── Header segment: accumulate product bullets ────────────────
        if seg == 'header':
            continue

        # ── Named section transition ──────────────────────────────────
        if seg != _current_seg:
            _flush_para()
            if ifu_texts:
                doc.nodes.append(IFUNode(
                    text=_clean(' '.join(ifu_texts))))
                ifu_texts = []
            _close_section()
            last_y    = top
            last_page = page
            _current_seg = seg
            if seg == 'footer':
                doc.nodes.append(FooterNode(text=text))
                continue
            elif seg == 'applicable_products':
                current_section = SectionNode(
                    heading='Applicable Products', page=page, top=top)
            elif seg not in ('header', 'ifu'):
                current_section = SectionNode(
                    heading=seg, page=page, top=top)
                continue


        # ── IFU segment: accumulate boilerplate text ──────────────────
        if seg == 'ifu':
            ifu_texts.append(text)
            continue

        # ── Footer segment: accumulate text into FooterNode ────────────
        if seg == 'footer':
            if doc.nodes and isinstance(doc.nodes[-1], FooterNode):
                doc.nodes[-1].text += ' ' + text
            continue

        if current_section is None:
            continue

        # ── Content accumulation (unchanged from here) ────────────────
        sub_name = classify_subsection(line)

        if _is_mm_family and not sub_name:
            _cur_sec_heading = (current_section.heading.lower()
                                if current_section else '')
            mm_result = classify_mm_subsection(
                line, 
                current_section=_cur_sec_heading)
            if mm_result:
                mm_text, mm_level = mm_result
                _flush_para()
                if mm_level == 1:
                    _close_sub()
                    current_sub = SubsectionNode(
                        heading=mm_text, page=page, top=top)
                    last_y = top
                    last_page = page
                    continue
                elif mm_level == 2:
                    _node = SubSubsectionNode(heading=mm_text)
                    if current_sub is not None:
                        current_sub.children.append(_node)
                    elif current_section is not None:
                        current_section.children.append(_node)
                    last_y = top
                    last_page = page
                    continue

        # If no match, try combining with previous bold fragment
        if not sub_name and _sub_fragment is not None:
            if line['bold'] and top - _sub_frag_y <= 10:
                combined = _sub_fragment + line['text']
                sub_name = classify_subsection({**line, 'text': combined})
            _sub_fragment = None
            _sub_frag_y   = 0.0

        # Save as fragment if bold, short, all-caps, and could be start of a subsection
        if (not sub_name and line['bold'] and
                line['text'].replace(' ', '').isupper() and
                len(line['text'].strip()) <= 6):
            _sub_fragment = line['text']
            _sub_frag_y   = top
            continue

        if sub_name:
            _flush_para()
            _close_sub()
            current_sub = SubsectionNode(heading=sub_name, page=page, top=top)
            last_y    = -999.0
            last_page = page
            continue

        subsub_name = classify_subsubsection(line)
        if subsub_name:
            _flush_para()
            _node = SubSubsectionNode(heading=subsub_name)
            if current_sub is not None:
                current_sub.children.append(_node)
            elif current_section is not None:
                current_section.children.append(_node)
            last_y    = top
            last_page = page
            continue

        # Skip table interior lines — except in References section
        # where tables are just formatted citations we want as body text
        if line.get('in_table') and not (
                current_section and
                'reference' in current_section.heading.lower()):
            continue

        if line['size'] <= 9.5 and not line['bold']:
            # Check if line is a table footnote: immediately below a table bbox,
            # non-bold, and within 30pt of a table bottom on the same page
            _near_table_bottom = any(
                0 <= top - tbot <= 30
                for tbot in _table_bottoms.get(page, [])
            )
            is_true_footnote = (
                # Traditional small-font footnote markers
                bool(re.match(r'^[A-Z]{2,5}\s*[\u2013\-]', text))
                or text.startswith('*')
                or text.startswith('\u2020')
                # Table footnote: immediately below a table, smaller than body text
                or (_near_table_bottom and not line['bold'] and line['size'] <= 9.5)
                # Page-bottom footnote (near footer zone) with small font
                or (top > 675 and line['size'] <= 8.5)
            )
            if is_true_footnote:
                _flush_para()
                tgt = _target()
                if tgt and isinstance(tgt[-1], FootnoteNode) and top - last_y <= 32:
                    tgt[-1].text += ' ' + text
                else:
                    tgt.append(FootnoteNode(text=text))
                last_y    = top
                last_page = page
                continue

        if para_lines and page == last_page and (top - last_y) > 20:
            _flush_para()

        last_y    = top
        last_page = page
        # Flush before a table title line so it becomes its own paragraph
        if (line['bold'] and re.match(r'Table\s+\d+[.\s]', text)):
            _flush_para()
        _accumulate(page, top)

    _flush_para()
    if ifu_texts:
        doc.nodes.append(IFUNode(text=_clean(' '.join(ifu_texts))))
    _close_section()
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
    doc = parse(Path(args.pdf).expanduser())
    if args.out:
        buf = io.StringIO()
        old = _sys.stdout; _sys.stdout = buf
        _print_tree(doc)
        _sys.stdout = old
        Path(args.out).write_text(buf.getvalue(), encoding='utf-8')
        print(f"Tree written to: {args.out}", file=_sys.stderr)
    else:
        _print_tree(doc)
