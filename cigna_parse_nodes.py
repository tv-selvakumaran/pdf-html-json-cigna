#!/usr/bin/env python3
"""
cigna_parse_nodes.py
====================
Node dataclasses, constants, and shared helpers for the Cigna V5 converter.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from reconstruct_cigna_bullet import ParagraphBlock
from cigna_extractor import construct_phrases, extract_citations
from cigna_parse_headings import normalize_heading
from cigna_constants import FOOTER_PATTERNS


# ════════════════════════════════════════════════════════════════════════════
# Node dataclasses
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Node:
    kind: str = 'node'

@dataclass
class ParagraphBlockNode(Node):
    block: ParagraphBlock = field(default_factory=ParagraphBlock)
    page: int   = 0
    top:  float = 0.0
    end_page: int = 0
    end_bottom: float = 0.0
    kind: str = 'paragraph_block'

@dataclass
class ReferenceItemNode(Node):
    item_num: int | None = None
    ref_type: str | None = None
    text: str = ''
    kind: str = 'reference_item'

@dataclass
class ReferencesNode(Node):
    heading: str = 'References'
    items: list = field(default_factory=list)  # list[ReferenceItemNode]
    kind: str = 'references'

@dataclass
class TableRowNode(Node):
    cells: list = field(default_factory=list)      # ordered cell values, already consolidated (nonempty-column-collapsed)
    row_kind: str = 'data'       # 'header' | 'header1' | 'header2' | 'subgroup' | 'data'
    kind: str = 'table_row'

@dataclass
class TableNode(Node):
    table_type: str = 'generic'
    page: int = 0
    top: float = 0.0
    title: str = ''
    footnote: str = ''
    header_rows: list[TableRowNode] = field(default_factory=list)
    rows: list[TableRowNode] = field(default_factory=list)
    html: str = ''   # rendered once, cached — or computed lazily from header_rows+rows
    end_page: int | None = None
    kind: str = 'table_node'

@dataclass
class FootnoteNode(Node):
    text: str = ''
    kind: str = 'footnote'

@dataclass
class IFUNode(Node):
    text: str = ''
    heading: str = 'Instructions for Use'
    kind: str = 'ifu'

@dataclass
class TOCNode(Node):
    entries: list = field(default_factory=list)  # list of (section_name, page_num)
    kind: str = 'toc'

@dataclass
class RelatedResourcesNode(Node):
    entries: list = field(default_factory=list)  # list of resource names
    kind: str = 'related_resources'

@dataclass
class SubsectionNode(Node):
    heading: str = ''
    children: list = field(default_factory=list)
    kind: str = 'subsection'
    page: int = 0
    top: float = 0.0

@dataclass
class SubSubsectionNode(Node):
    heading: str = ''
    children: list = field(default_factory=list)
    page: int = 0
    top:  float = 0.0
    kind: str = 'subsubsection'

@dataclass
class SectionNode(Node):
    heading: str = ''
    children: list = field(default_factory=list)
    kind: str = 'section'
    page: int = 0      # page number where this section heading appears
    top:  float = 0.0  # y-coordinate of heading (for bbox comparison)

@dataclass
class ParagraphBlock:
    plain_text: str = ''
    items: list = field(default_factory=list)
    phrases: list = field(default_factory=list)
    zone: str | None = None
    society_names: list = field(default_factory=list)
    citation_links: list = field(default_factory=list)

@dataclass
class HeaderNode(Node):
    meta: dict = field(default_factory=dict)
    product_bullets: list = field(default_factory=list)
    kind: str = 'header'

@dataclass
class FooterNode(Node):
    text: str = ''
    kind: str = 'footer'

@dataclass
class CignaDoc:
    meta: dict = field(default_factory=dict)
    nodes: list = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ════════════════════════════════════════════════════════════════════════════

def clean(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '–').replace('\u2014', '—')
    text = text.replace('\u00a0', ' ')
    return text

def is_boilerplate(text: str) -> bool:
    tl = text.lower()
    return any(f in tl for f in BOILERPLATE_FRAGMENTS)

def is_page_footer(top: float, text: str) -> bool:
    return any(p.match(text) for p in FOOTER_PATTERNS)


def identify_paragraph_boundaries(
    entries: list,
    seg_start: tuple,
    seg_end: tuple,
    table_bboxes: set,
    gap_threshold: float = 18.0,
    is_mm_family: bool = False,
    is_cpg_family: bool = False,
    current_section: str = '',
    include_start: bool = False,
) -> list[dict]:
    from cigna_constants import FOOTER_PATTERNS

    _table_ranges = list(table_bboxes)

    def _in_table(pg, top):
        for sp, st, en_st, ep, eb, en_eb in _table_ranges:
            if sp == ep:
                # Single page table
                if pg == sp and en_st <= top <= en_eb:
                    return True
            else:
                # Multi-page table
                if pg == sp and top >= en_st:
                    return True
                if pg == ep and top <= en_eb:
                    return True
                if sp < pg < ep:
                    return True
        return False

    start_page, start_top = seg_start
    end_page, end_top = seg_end

    # Separate entries into content entries and subsection heading entries
    # maintaining page order
    all_items = []  # mixed list of entries and subsection dicts
    for e in entries:
        pg, top, etype, text, x0, size, bold, underline, leading_bold, italic = e
        if pg < start_page or pg > end_page:
            continue
        if pg == start_page and (top < start_top if include_start else top <= start_top):
            continue
        if pg == end_page and top >= end_top:
            continue
        if _in_table(pg, top):
            continue
        if any(p.match(text.strip()) for p in FOOTER_PATTERNS):
            continue
        all_items.append(('entry', e))

    if not all_items:
        return []

    # Process items in order — group entries into paragraphs,
    # emit subsection items as-is
    result = []

    def _flush_group(grp_entries, grp_start_pg, grp_start_top,
                     end_pg, end_top_val, end_size):
        if grp_entries:
            result.append({
                'type': 'paragraph',
                'start_page': grp_start_pg,
                'start_top': grp_start_top,
                'end_page': end_pg,
                'end_bottom': end_top_val + end_size,
            })

    def is_subsection_heading(pg, top, etype, text, x0, size, 
                               bold, underline, italic, prev_gap):
        """Return (level, heading_text) if this entry is a subsection heading,
        else None. Only called for plain entries with large preceding gap."""
        if not bold and not italic:
            return None
        if prev_gap <= gap_threshold:
            return None  # not standalone — part of paragraph
        
        # Make a line dict for classifiers
        line = {
            'text': text, 'bold': bold, 'underline': underline,
            'size': size, 'x0': x0, 'top': top,
            'italic': italic, 'in_table': False,
            'in_section_rect': False,
        }
        
        if is_cpg_family:
            from cigna_parse_headings import classify_cpg_subsection
            cpg_result = classify_cpg_subsection(line, current_section=current_section)
            if cpg_result:
                return cpg_result
        elif is_mm_family:
            from cigna_parse_headings import classify_mm_subsection
            mm_result = classify_mm_subsection(line, current_section=current_section)
            if mm_result:
                return mm_result
        else:
            from cigna_parse_headings import classify_drug_subsection
            drug_result = classify_drug_subsection(
                line, current_section=current_section)
            if drug_result:
                return drug_result
        
        from cigna_parse_headings import classify_subsection
        sub = classify_subsection(line)
        if sub:
            return (sub, 2)

        from cigna_parse_headings import classify_subsubsection
        subsub = classify_subsubsection(line)
        if subsub:
            return (subsub, 2)
        
        return None


    def _try_two_line_subsection(idx, pg, top, etype, text, x0, size,
                                  bold, underline, italic):
        if idx + 1 >= len(all_items):
            return None
        _next_kind, _next_item = all_items[idx + 1]
        if _next_kind != 'entry':
            return None
        (n_pg, n_top, n_etype, n_text, n_x0, n_size,
         n_bold, n_underline, _, n_italic) = _next_item
        if n_etype != etype or n_bold != bold or n_pg != pg:
            return None
        if abs(n_size - size) > 0.5 or abs(n_x0 - x0) > 3.0:
            return None
        _combined_text = (text.rstrip() + ' ' + n_text.lstrip()).strip()
        _combined_line = {
            'text': _combined_text, 'bold': bold, 'underline': underline,
            'size': size, 'x0': x0, 'top': top, 'italic': italic,
            'in_table': False, 'in_section_rect': False,
        }
        from cigna_parse_headings import classify_drug_subsection
        _result = classify_drug_subsection(_combined_line, current_section=current_section)
        if _result:
            return (_result[0], _result[1])
        return None

    # State for grouping
    grp_entries   = []
    grp_start_pg  = None
    grp_start_top = None
    prev_pg       = None
    prev_top      = None
    prev_etype    = None
    prev_x0       = None
    prev_size     = 10.0
    prev_text     = ''
    bullet_x0     = None
    in_bullet_context = False

    idx = 0
    while idx < len(all_items):
        kind, item = all_items[idx]

        if kind == 'subsection':
            # Flush current paragraph group first
            if grp_entries:
                _flush_group(grp_entries, grp_start_pg, grp_start_top,
                             prev_pg, prev_top, prev_size)
                grp_entries = []
                grp_start_pg = None
                grp_start_top = None
                bullet_x0 = None
                in_bullet_context = False
            result.append(item)
            idx += 1
            continue

        # Content entry
        pg, top, etype, text, x0, size, bold, underline, leading_bold, italic = item
        gap = top - prev_top if (prev_pg is not None and pg == prev_pg) else 9999

        new_para = False

        if grp_start_pg is None:
            # First entry — check if it's a subsection heading
            sub_result = is_subsection_heading(
                pg, top, etype, text, x0, size, bold, underline, italic, 9999)
            _consumed_extra = False
            if not sub_result and etype == 'plain':
                _two_line = _try_two_line_subsection(
                    idx, pg, top, etype, text, x0, size, bold, underline, italic)
                if _two_line:
                    sub_result = _two_line
                    _consumed_extra = True
            if sub_result and etype in ('plain',):
                sub_text, sub_level = sub_result
                result.append({
                    'type': 'subsection',
                    'level': sub_level,
                    'heading': sub_text,
                    'page': pg,
                    'top': top,
                })
                if _consumed_extra:
                    _, _next_item = all_items[idx + 1]
                    (prev_pg, prev_top, prev_etype, prev_text, prev_x0,
                     prev_size, _, _, _, _) = _next_item
                    idx += 2
                else:
                    prev_pg = pg
                    prev_top = top
                    prev_etype = etype
                    prev_x0 = x0
                    prev_text = text
                    prev_size = size
                    idx += 1
                continue
            # Not a subsection — start group normally
            grp_start_pg = pg
            grp_start_top = top
            in_bullet_context = etype in ('bullet', 'sub', 'subsub',
                                          'sub_bullet', 'sub_sub_bullet', 'num')
            bullet_x0 = x0 if etype == 'bullet' else None
        else:
            # Determine if new paragraph
            if etype in ('sub', 'subsub', 'sub_bullet', 'sub_sub_bullet',
                         'letter', 'roman', 'paren_num'):
                new_para = False
                in_bullet_context = True

            elif etype == 'bullet':
                if _table_between(prev_pg, prev_top, pg, top, _table_ranges):
                    new_para = True
                    in_bullet_context = False
                    bullet_x0 = None
                elif gap == 9999 and in_bullet_context:
                    new_para = False
                elif gap > gap_threshold and not in_bullet_context:
                    if prev_etype == 'plain' and prev_text.rstrip().endswith(':'):
                        new_para = False
                        in_bullet_context = True
                        bullet_x0 = x0
                    else:
                        new_para = True
                elif gap > gap_threshold and x0 < (bullet_x0 or 999) - 2:
                    new_para = True
                in_bullet_context = True
                bullet_x0 = x0

            elif etype in ('plain', 'note'):
                _crossed_page = (prev_pg is not None and pg != prev_pg)
                _prev_ends_hyphenated = bool(
                    prev_text and re.search(r'[A-Za-z]-$', prev_text.rstrip()))
                _prev_lacks_terminal_punct = bool(
                    prev_text and prev_text.rstrip() and
                    prev_text.rstrip()[-1] not in '.:;?!')
                _current_starts_lowercase = bool(text and text.strip() and text.strip()[0].islower())
                _looks_like_continuation = (
                    _prev_ends_hyphenated or
                    (_prev_lacks_terminal_punct and _current_starts_lowercase))
                if _crossed_page and _looks_like_continuation:
                    new_para = False
                elif gap > gap_threshold:
                    # Check if this is a subsection heading
                    sub_result = is_subsection_heading(
                        pg, top, etype, text, x0, size,
                        bold, underline, italic, gap)
                    _consumed_extra = False
                    if not sub_result:
                        _two_line = _try_two_line_subsection(
                            idx, pg, top, etype, text, x0, size, bold, underline, italic)
                        if _two_line:
                            sub_result = _two_line
                            _consumed_extra = True
                    if sub_result:
                        sub_text, sub_level = sub_result
                        # Flush current paragraph
                        if grp_entries:
                            _flush_group(grp_entries, grp_start_pg, grp_start_top,
                                         prev_pg, prev_top, prev_size)
                            grp_entries = []
                            grp_start_pg = None
                            grp_start_top = None
                            bullet_x0 = None
                            in_bullet_context = False
                        # Emit subsection item
                        result.append({
                            'type': 'subsection',
                            'level': sub_level,
                            'heading': sub_text,
                            'page': pg,
                            'top': top,
                        })
                        if _consumed_extra:
                            _, _next_item = all_items[idx + 1]
                            (prev_pg, prev_top, prev_etype, prev_text, prev_x0,
                             prev_size, _, _, _, _) = _next_item
                            idx += 2
                        else:
                            prev_pg = pg
                            prev_top = top
                            prev_etype = etype
                            prev_x0 = x0
                            prev_text = text
                            prev_size = size
                            idx += 1
                        continue
                    elif _table_between(prev_pg, prev_top, pg, top, _table_ranges):
                        new_para = True
                        in_bullet_context = False
                        bullet_x0 = None
                    elif in_bullet_context and x0 >= (bullet_x0 or 0) - 5:
                            new_para = False
                    else:
                        new_para = True
                        in_bullet_context = False
                        bullet_x0 = None

            elif etype == 'num':
                if _table_between(prev_pg, prev_top, pg, top, _table_ranges):
                    new_para = True
                    in_bullet_context = False
                elif gap > gap_threshold and not in_bullet_context:
                    new_para = True
                in_bullet_context = True

            if new_para:
                _flush_group(grp_entries, grp_start_pg, grp_start_top,
                             prev_pg, prev_top, prev_size)
                grp_entries = []
                grp_start_pg = pg
                grp_start_top = top
                in_bullet_context = etype in ('bullet', 'sub', 'subsub',
                                              'sub_bullet', 'sub_sub_bullet', 'num')
                bullet_x0 = x0 if etype == 'bullet' else None

        grp_entries.append(item)
        prev_pg    = pg
        prev_top   = top
        prev_etype = etype
        prev_x0    = x0
        prev_text  = text
        prev_size  = size
        idx += 1

    # Flush last group
    if grp_entries:
        _flush_group(grp_entries, grp_start_pg, grp_start_top,
                     prev_pg, prev_top, prev_size)

    return result


def _table_between(prev_pg, prev_top, pg, top, table_ranges):
        """True if any table span lies strictly between the previous
        kept entry's position and the current entry's position --
        i.e. a table interrupted the flow, so this can never be a
        continuation of the same paragraph/item, no matter what
        in_bullet_context says."""
        if prev_pg is None:
            return False
        for sp, st, en_st, ep, eb, en_eb in table_ranges:
            # table starts at or after prev position, and ends at or
            # before current position
            starts_after_prev = (sp > prev_pg or (sp == prev_pg and en_st >= prev_top))
            ends_before_cur   = (ep < pg or (ep == pg and en_eb <= top))
            if starts_after_prev and ends_before_cur:
                return True
        return False


def _convert_references_section(doc: CignaDoc) -> None:
    """
    Post-processing pass: find a SectionNode(heading='References') and
    convert its ParagraphBlockNode/NumItem content into a proper
    ReferencesNode with ReferenceItemNode children -- letting the
    section build normally through the existing citation-numbering
    fix pipeline first, then restructuring the result.
    """
    for i, node in enumerate(doc.nodes):
        if (type(node).__name__ == 'SectionNode' and
                (node.heading or '').strip().lower() == 'references'):
            items = []
            for child in node.children:
                if type(child).__name__ != 'ParagraphBlockNode':
                    continue
                block = getattr(child, 'block', None)
                if block is None:
                    continue
                for it in getattr(block, 'items', []) or []:
                    if type(it).__name__ != 'NumItem':
                        continue
                    text = (getattr(it, 'text', '') or '').strip()
                    if not text:
                        continue
                    items.append(ReferenceItemNode(
                        item_num=getattr(it, 'number', None),
                        ref_type=None,
                        text=text,
                    ))
            doc.nodes[i] = ReferencesNode(heading=node.heading, items=items)
            break


def _zone_for_heading(heading_text: str) -> tuple[str, str] | None:
    """
    Map a recognized subsection/sub-subsection heading to a
    (heading_zone, content_zone) pair: heading_zone tags the one
    paragraph where this heading itself was detected; content_zone is
    what propagates to subsequent ordinary paragraphs until the next
    heading changes it.
    """
    norm = normalize_heading(heading_text)
    if norm == 'u.s. food and drug administration (fda)':
        return ('fda_heading', 'fda_content')
    if norm == 'literature review':
        return ('literature_review_heading', 'literature_review_content')
    if norm == 'professional societies/organizations':
        return ('professional_societies_heading', 'professional_societies_content')
    return ('topic_heading', 'topic_heading')  # topic headings have no
                                                 # distinct ambient
                                                 # content zone of
                                                 # their own; the
                                                 # overview paragraph
                                                 # right after IS the
                                                 # topic content


def _tag_one_paragraph_zone(block, page: int, top: float,
                             end_page: int, end_bottom: float, _pdf: PDF,
                             current_section: str, current_subsection: str,
                             fallback_zone: str) -> str:
    """
    Compute phrases for one paragraph and determine its zone. Returns
    the zone to use as the fallback for the NEXT ordinary paragraph
    (i.e. this paragraph's own resulting zone, so consecutive prose
    paragraphs inherit the most recently seen heading's zone).
    """
    if page == end_page:
        _page_obj = _pdf.pages[page - 1]
        _ranges = [(_page_obj, top, end_bottom)]
    else:
        _ranges = []
        for _pg in range(page, end_page + 1):
            _page_obj = _pdf.pages[_pg - 1]
            _rt = top if _pg == page else 0
            _rb = end_bottom if _pg == end_page else 9999
            _ranges.append((_page_obj, _rt, _rb))

    block.phrases = construct_phrases(_ranges)

    from cigna_parse_headings import classify_mm_general_background_subsubsection
    _subsec_match = classify_mm_general_background_subsubsection(
        block.phrases, 0,
        current_section=current_section,
        current_subsection=current_subsection)
    if _subsec_match:
        block.zone = 'professional_society_opinion'
        _heading_text = _subsec_match[0]
        block.society_names = [s.strip() for s in _heading_text.split('/') if s.strip()]
        return fallback_zone  # a society-opinion paragraph doesn't
                                # change the "ambient" zone for
                                # subsequent ordinary paragraphs

    block.zone = fallback_zone
    return fallback_zone


def _tag_general_background_zones(doc: 'CignaDoc', _pdf: PDF) -> None:
    """
    Post-processing pass: for each General Background/Background
    SectionNode, walk its children (SubsectionNode/SubSubsectionNode/
    ParagraphBlockNode, nested and in document order), tag every
    ParagraphBlockNode's block with a `.zone` (and `.society_name`
    where applicable). Additive only -- does not alter structure.
    """
    for node in doc.nodes:
        if not (type(node).__name__ == 'SectionNode' and
                (node.heading or '').strip().lower() in ('general background', 'background')):
            continue

        state = {'zone': 'introduction'}

        def _walk(children, subsection_heading):
            for child in children:
                _tname = type(child).__name__
                if _tname == 'SubsectionNode':
                    _mapped = _zone_for_heading(child.heading)
                    if _mapped:
                        state['zone'] = _mapped[1]  # content zone propagates
                    _walk(child.children, child.heading)
                elif _tname == 'SubSubsectionNode':
                    _mapped = _zone_for_heading(child.heading)
                    if _mapped:
                        state['zone'] = _mapped[1]
                        subsection_heading = child.heading
                    _walk(child.children, subsection_heading)
                elif _tname == 'ParagraphBlockNode':
                    block = getattr(child, 'block', None)
                    if block is None:
                        continue
                    state['zone'] = _tag_one_paragraph_zone(
                        block, child.page, child.top,
                        child.end_page, child.end_bottom, _pdf,
                        current_section=node.heading,
                        current_subsection=subsection_heading,
                        fallback_zone=state['zone'])

        _walk(node.children, '')



def _citation_signature(mention: str) -> tuple | None:
    """
    Reduce a matched citation mention (e.g. 'Payne, et al., 2025' or
    'Homsi and Gaffey, 2022' or 'American Rhinologic Society, 2019')
    to a normalized (first_name_token, year) signature for lookup
    purposes -- using only the FIRST author/org name token, since
    that's what's consistently present in both the in-text mention
    and the reference list's own citation text.
    """
    _m = re.match(r'^(.*?),?\s+(\d{4})$', mention.strip())
    if not _m:
        return None
    _name_part, _year = _m.groups()
    # Take just the first capitalized word/phrase before any
    # "et al."/"and"/comma, as the stable matching key.
    _first_token = re.split(r'\s+(?:et\s+al\.?|and|&)\b|,', _name_part)[0].strip()
    if not _first_token:
        return None
    return (_first_token.lower(), _year)


def _link_general_background_citations(doc: 'CignaDoc', reference_lookup: dict) -> None:
    """
    Post-processing pass: for each General Background/Background
    SectionNode, walk its ParagraphBlockNode children (nested under
    SubsectionNode/SubSubsectionNode as needed) and, for paragraphs
    tagged with a zone where citations are expected (literature
    review content, professional society opinions), extract and link
    their in-text citation mentions via reference_lookup.
    """
    _CITABLE_ZONES = {'literature_review_content', 'professional_society_opinion',
                       'introduction', 'topic_heading'}
    # (introduction/topic_heading included since your original
    # description noted Zone 1 also references citations, e.g.
    # "Seidman, et al., 2015; Soler et al. (2010)")

    for node in doc.nodes:
        if not (type(node).__name__ == 'SectionNode' and
                (node.heading or '').strip().lower() in ('general background', 'background')):
            continue

        def _walk(children):
            for child in children:
                _tname = type(child).__name__
                if _tname in ('SubsectionNode', 'SubSubsectionNode'):
                    _walk(child.children)
                elif _tname == 'ParagraphBlockNode':
                    block = getattr(child, 'block', None)
                    if block is None:
                        continue
                    if getattr(block, 'zone', None) in _CITABLE_ZONES:
                        _link_citations_for_paragraph(block, reference_lookup)

        _walk(node.children)



def _extract_reference_signature(ref_text: str) -> tuple | None:
    """
    Extract a (first_author_surname, year) signature from a formal
    reference citation's full text (e.g. '15. Payne SC, McKenna M, ...
    Otolaryngol Head Neck Surg. 2025 Aug;173 Suppl 1:S1-S56.') --
    a different shape than the short in-text mention style, so this
    uses independent extraction for the surname and the year rather
    than one combined regex.
    """
    _text = re.sub(r'^\d+[.)]\s*', '', ref_text.strip())

    # First author's surname: the first capitalized word before a
    # comma, PROVIDED the text doesn't start with a known
    # organization name (checked separately).
    from cigna_constants import CITATION_ORG_NAMES
    for full_name, abbr in CITATION_ORG_NAMES:
        _stripped = text.lower().lstrip('0123456789.) ')
        if _stripped.startswith(full_name.lower()) or _stripped.startswith(abbr.lower()):
            _year_m = re.search(r'\b(19\d{2}|20\d{2})\b', text)
            if _year_m:
                return (frozenset({full_name.lower()}), _year_m.group(1))
            return None
    else:
        _m = re.match(r'^((?:[A-Z]\s+)?[A-Z][A-Za-zÀ-ÿ\-]+)', _text)
        _surname = _m.group(1) if _m else None

    if not _surname:
        return None

    # Year: first plausible 4-digit year found anywhere in the text.
    _year_m = re.search(r'\b(19\d{2}|20\d{2})\b', _text)
    if not _year_m:
        return None

    return (_surname.lower(), _year_m.group(1))



def _build_reference_lookup(doc: 'CignaDoc') -> list[tuple]:
    """
    Walk doc.nodes to find the ReferencesNode, and build a list of
    (item_num, author_surnames, year) tuples -- one per reference,
    used for subset-matching against in-text citation mentions.
    """
    from cigna_extractor import _extract_authors_and_year
    lookup = []
    for node in doc.nodes:
        if type(node).__name__ != 'ReferencesNode':
            continue
        for item in node.items:
            item_num = getattr(item, 'item_num', None)
            text = getattr(item, 'text', '') or ''
            if item_num is None or not text:
                continue
            _sig = _extract_authors_and_year(text, is_reference=True)
            if _sig:
                lookup.append((item_num, _sig[0], _sig[1]))
        break
    return lookup


def _find_matching_reference(mention: str, lookup: list[tuple]) -> int | None:
    """
    Given an in-text citation mention, find the reference item_num
    whose author set is a superset of the mention's authors and whose
    year matches. Returns None if no reference matches, or if more
    than one reference matches ambiguously (rare; left unresolved
    rather than guessing).
    """
    from cigna_extractor import _extract_authors_and_year, _citation_matches_reference
    _mention_sig = _extract_authors_and_year(mention, is_reference=False)
    if not _mention_sig:
        return None

    _candidates = [
        item_num for item_num, authors, year in lookup
        if _citation_matches_reference(_mention_sig, (authors, year))
    ]
    if len(_candidates) == 1:
        return _candidates[0]
    return None  # zero or ambiguous matches


def _link_citations_for_paragraph(block, reference_lookup: list) -> None:
    """
    Extract citation mentions from a paragraph's text and link each
    to a reference item_num via reference_lookup. Sets
    block.citation_links as a list of {mention, item_num} dicts
    (item_num is None if no unambiguous match was found).
    """
    _text = block.plain_text or ' '.join(
        getattr(it, 'text', '') for it in getattr(block, 'items', []) if getattr(it, 'text', ''))
    if not _text:
        return

    _links = []
    for mention in extract_citations(_text):
        item_num = _find_matching_reference(mention, reference_lookup)
        _links.append({'mention': mention, 'item_num': item_num})
    if _links:
        block.citation_links = _links


