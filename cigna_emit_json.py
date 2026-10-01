"""
cigna_emit_json.py
==================
Emit a JSON-serializable dict directly from a CignaDoc tree.

Usage:
    from cigna_parse import parse
    from cigna_emit_json import emit_json

    doc, _ = parse(pdf_path)
    data = emit_json(doc)
"""

from cigna_parse_nodes import (
    SectionNode, SubsectionNode, SubSubsectionNode,
    ParagraphBlockNode, TableNode, FootnoteNode,
    IFUNode, FooterNode, HeaderNode, TOCNode,
    ReferencesNode,
)
from reconstruct_cigna_bullet import (
    BulletItem, SubBulletItem, SubSubItem,
    NumItem, LetterItem, RomanItem, NoteItem, PlainText,
)


# ── Bullet helpers ────────────────────────────────────────────────────────────

def _roman_to_dict(ri):
    return {'type': 'roman', 'text': ri.text}


def _letter_to_dict(li):
    return {
        'type': 'letter',
        'text': li.text,
        'children': [_roman_to_dict(ri) for ri in getattr(li, 'children', [])]
    }


def _num_to_dict(ni):
    return {
        'type': 'num',
        'text': ni.text,
        'children': [_letter_to_dict(li) for li in getattr(ni, 'children', [])]
    }


def _sub_bullet_to_dict(sub):
    return {
        'type': 'sub_bullet',
        'text': sub.text,
        'children': [
            {'type': 'sub_sub_bullet', 'text': ssub.text}
            for ssub in getattr(sub, 'children', [])
        ]
    }


def _bullet_to_dict(item):
    return {
        'type': 'bullet',
        'text': item.text,
        'children': [_sub_bullet_to_dict(sub) for sub in item.children]
    }


def _items_to_structured(items):
    """Convert block items list to structured_content list."""
    result = []
    for item in items:
        if isinstance(item, BulletItem):
            result.append(_bullet_to_dict(item))
        elif isinstance(item, NumItem):
            result.append(_num_to_dict(item))
        elif isinstance(item, NoteItem):
            result.append({'type': 'note', 'text': item.text})
        elif isinstance(item, PlainText):
            result.append({'type': 'text', 'text': item.text})
    return result


# ── Node converters ───────────────────────────────────────────────────────────

def _para_to_dict(n, order):
    block = n.block
    structured = _items_to_structured(block.items)
    return {
        'paragraph_order': order,
        'content': block.plain_text or '',
        'content_type': 'paragraph',
        'structured_content': structured if structured else None,
    }


def _footnote_to_dict(n, order):
    return {
        'paragraph_order': order,
        'content': n.text,
        'content_type': 'footnote',
        'structured_content': None,
    }


def _table_to_dict(n):
    rows = getattr(n, 'rows', None)
    return {
        'table_type': n.table_type,
        'title': n.title or '',
        'footnote': n.footnote or '',
        'html': n.html,
        'rows': [
            {'cells': r.cells, 'row_kind': r.row_kind}
            for r in rows
        ] if rows else None,
    }


def _children_to_dict(children):
    """
    Walk a list of mixed children and separate into:
      paragraphs, tables, subsections

    Key insight from debug: subsection headings and their following
    paragraphs are siblings at the same level — paragraphs after a
    subsection heading belong to that subsection.
    """
    paragraphs = []
    tables = []
    subsections = []
    para_order = 1
    current_sub = None  # track last subsection seen

    for n in children:
        if isinstance(n, (SubsectionNode, SubSubsectionNode)):
            current_sub = _subsection_to_dict(n)
            subsections.append(current_sub)
        elif isinstance(n, ParagraphBlockNode):
            pd = _para_to_dict(n, para_order)
            para_order += 1
            if current_sub is not None:
                # paragraph belongs to the preceding subsection
                current_sub['paragraphs'].append(pd)
            else:
                paragraphs.append(pd)
        elif isinstance(n, TableNode):
            td = _table_to_dict(n)
            if current_sub is not None:
                current_sub['tables'].append(td)
            else:
                tables.append(td)
        elif isinstance(n, FootnoteNode):
            fd = _footnote_to_dict(n, para_order)
            para_order += 1
            if current_sub is not None:
                current_sub['paragraphs'].append(fd)
            else:
                paragraphs.append(fd)

    return paragraphs, tables, subsections


def _subsection_to_dict(n):
    level = 1 if isinstance(n, SubsectionNode) else 2
    return {
        'label': n.heading,
        'level': level,
        'paragraphs': [],   # filled by _children_to_dict of parent
        'tables': [],       # filled by _children_to_dict of parent
        'subsections': [],  # filled by _children_to_dict of parent
    }


def _section_to_dict(n, order):
    paragraphs, tables, subsections = _children_to_dict(n.children)
    return {
        'section_type': n.heading.lower().replace(' ', '_'),
        'title': n.heading,
        'section_order': order,
        'paragraphs': paragraphs,
        'tables': tables,
        'subsections': subsections,
    }


# ── References ────────────────────────────────────────────────────────────────

def _extract_references(section):
    """Extract references from a References SectionNode."""
    refs = []
    order = 1
    for child in section.children:
        if isinstance(child, ParagraphBlockNode):
            # References are NumItems in block.items
            for item in child.block.items:
                if isinstance(item, NumItem):
                    refs.append({
                        'item_num': order,
                        'ref_type': 'citation',
                        'text': item.text,
                    })
                    order += 1
            # Also handle plain_text if no items
            if not child.block.items and child.block.plain_text:
                refs.append({
                    'item_num': order,
                    'ref_type': 'citation',
                    'text': child.block.plain_text,
                })
                order += 1
    return refs


def _references_to_dict(n):
    return {
        'section_type': 'references',
        'title': n.heading,
        'items': [
            {'item_num': it.item_num, 'ref_type': it.ref_type, 'text': it.text}
            for it in n.items
        ],
    }


# ── Applicable Products ───────────────────────────────────────────────────────

def _extract_applicable_products(section):
    """Extract product bullet list from Applicable Products SectionNode."""
    products = []
    for child in section.children:
        if isinstance(child, ParagraphBlockNode):
            for item in child.block.items:
                if isinstance(item, BulletItem):
                    products.append(item.text)
    return products


# ── Main emitter ──────────────────────────────────────────────────────────────

def emit_json(doc) -> dict:
    """
    Convert a CignaDoc tree directly to a JSON-serializable dict.

    Returns a dict ready for json.dumps() or database insertion.
    """
    result = {
        'title':            doc.meta.get('title', ''),
        'policy_number':    doc.meta.get('policy_id', ''),
        'effective_date':   doc.meta.get('publish_date', ''),
        'next_review_date': doc.meta.get('next_review_date', ''),
        'last_review_date': doc.meta.get('last_review_date', ''),
        'policy_type':      doc.meta.get('policy_type', ''),
        'category':         doc.meta.get('doc_type', ''),
        'source_url':       doc.meta.get('source_url', ''),
        'applicable_products': [],
        'ifu_text':         '',
        'footer_text':      '',
        'sections':         [],
        'references':       [],
    }

    sec_order = 1
    for n in doc.nodes:
        if isinstance(n, HeaderNode):
            pass  # metadata already in doc.meta

        elif isinstance(n, IFUNode):
            result['ifu_text'] = n.text

        elif isinstance(n, FooterNode):
            result['footer_text'] = n.text

        elif isinstance(n, TOCNode):
            pass  # skip table of contents

        elif isinstance(n, ReferencesNode):
            result['references'] = [
                {'item_num': it.item_num, 'ref_type': 'citation', 'text': it.text}
                for it in n.items
            ]

        elif isinstance(n, SectionNode):
            heading_lc = n.heading.lower()

            if heading_lc == 'applicable products':
                result['applicable_products'] = _extract_applicable_products(n)

            elif 'reference' in heading_lc:
                result['references'] = _extract_references(n)

            else:
                result['sections'].append(_section_to_dict(n, sec_order))
                sec_order += 1

    return result
