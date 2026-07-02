#!/usr/bin/env python3
"""
cigna_emit.py  —  V3: Document Tree → HTML
"""

from __future__ import annotations
import re
from html import escape as esc
from pathlib import Path

from cigna_parse import (
    CignaDoc, HeaderNode, SectionNode, SubsectionNode, 
    SubSubsectionNode, ParagraphBlockNode, TableNode, 
    FootnoteNode, IFUNode, FooterNode, 
    TOCNode, RelatedResourcesNode,
)
from reconstruct_cigna_bullet import (
    BulletItem, SubBulletItem, SubSubItem,
    NumItem, LetterItem, RomanItem, NoteItem, PlainText, ParagraphBlock,
)


# ════════════════════════════════════════════════════════════════════════════
# Section stripe
# ════════════════════════════════════════════════════════════════════════════

def _section_stripe(heading: str) -> str:
    return (
        f'  <table border="1" bordercolor="#000000" cellpadding="0" '
        f'cellspacing="0" style="width:100%">\n'
        f'   <tbody>\n    <tr>\n'
        f'     <td class="head_stripe" width="100%">'
        f'<strong>{esc(heading)}</strong></td>\n'
        f'    </tr>\n   </tbody>\n  </table>'
    )


# ════════════════════════════════════════════════════════════════════════════
# Paragraph block renderer
# ════════════════════════════════════════════════════════════════════════════

def _render_paragraph_block(block: ParagraphBlock, parts: list[str],
                             in_section: str = '') -> None:

    if block.plain_text:
        pt = block.plain_text
        if pt.startswith('Note:') or pt.startswith('Note :'):
            note_body = (pt.removeprefix('Note:')
                           .removeprefix('Note :').strip())
            parts_note = note_body.split('<br>')
            rendered = '<br>'.join(esc(p.strip()) for p in parts_note)
            parts.append(
                f'  <p style="margin-left:20px"><em>'
                f'<strong>Note:</strong> {rendered}</em></p>')
        else:
            parts.append(f'  <p>{esc(pt)}</p>')

    for item in block.items:
        if isinstance(item, BulletItem):
            parts.append(
                f'  <p style="margin-left:20px">• {esc(item.text)}</p>')
            for sub in item.children:
                parts.append(
                    f'  <p style="margin-left:40px">○ {esc(sub.text)}</p>')
                for ssub in sub.children:
                    parts.append(
                        f'  <p style="margin-left:60px">▪ {esc(ssub.text)}</p>')

        elif isinstance(item, NumItem):
            parts.append(
                f'  <p style="margin-left:20px">{esc(item.text)}</p>')
            # Notes at num level (appear right after num text)
            for ni in getattr(item, 'notes', []):
                note_body = (ni.text.removeprefix('Note:')
                             .removeprefix('Note :').strip())
                parts.append(
                    f'  <p style="margin-left:20px"><em>'
                    f'<strong>Note:</strong> {esc(note_body)}</em></p>')
            for li in item.children:
                if li.text:
                    parts.append(
                        f'  <p style="margin-left:40px">{esc(li.text)}</p>')
                for ri in li.children:
                    if ri.text:
                        parts.append(
                            f'  <p style="margin-left:60px">{esc(ri.text)}</p>')
                    for ni in ri.children:
                        note_body = (ni.text
                                     .removeprefix('Note:')
                                     .removeprefix('Note :')
                                     .strip())
                        parts.append(
                            f'  <p style="margin-left:60px"><em>'
                            f'<strong>Note:</strong> {esc(note_body)}'
                            f'</em></p>')

        elif isinstance(item, NoteItem):
            note_body = (item.text
                         .removeprefix('Note:')
                         .removeprefix('Note :')
                         .strip())
            # note_body may contain <br> separators for numbered items
            # Don't escape <br> tags — escape everything else
            parts_note = note_body.split('<br>')
            rendered = '<br>'.join(esc(p.strip()) for p in parts_note)
            parts.append(
                f'  <p style="margin-left:20px"><em>'
                f'<strong>Note:</strong> {esc(note_body)}'
                f'</em></p>')

        elif isinstance(item, PlainText):
            if item.text:
                text = item.text.strip()
                if text.startswith('Note:') or text.startswith('Note :'):
                    note_body = (text.removeprefix('Note:')
                                     .removeprefix('Note :').strip())
                    parts_note = note_body.split('<br>')
                    rendered = '<br>'.join(esc(p.strip()) for p in parts_note)
                    parts.append(
                        f'  <p style="margin-left:20px"><em>'
                        f'<strong>Note:</strong> {esc(note_body)}</em></p>')
                    continue
                # Detect orphan letter items A) B) C) and roman numerals
                elif re.match(r'^[A-E]\)', item.text):
                    parts.append(
                        f'  <p style="margin-left:40px">{esc(item.text)}</p>')
                elif re.match(r'^[ivxIVX]+\.', item.text):
                    parts.append(
                        f'  <p style="margin-left:60px">{esc(item.text)}</p>')
                elif (parts and parts[-1].startswith('  <p>') and
                      parts[-1].endswith('</p>') and
                      '<em>' not in parts[-1] and
                      '<strong>' not in parts[-1] and
                      not parts[-1].rstrip('</p>').rstrip().endswith(
                          ('.', ':', '?', '!', ';'))):
                    # Previous plain line didn't end a sentence — merge
                    parts[-1] = parts[-1][:-4] + ' ' + esc(item.text) + '</p>'
                else:
                    parts.append(f'  <p>{esc(item.text)}</p>')


# ════════════════════════════════════════════════════════════════════════════
# Node renderers
# ════════════════════════════════════════════════════════════════════════════

def _render_node(node, parts: list[str], in_section: str = '') -> None:

    if isinstance(node, HeaderNode):
        _render_header(node, parts)

    elif isinstance(node, IFUNode):
        parts.append(_section_stripe('Instructions for Use'))
        parts.append(f'  <p><em>{esc(node.text)}</em></p>')

    elif isinstance(node, ParagraphBlockNode):
        _render_paragraph_block(node.block, parts, in_section=in_section)

    elif isinstance(node, SectionNode):
        parts.append(_section_stripe(node.heading))
        sec = node.heading.lower()
        for child in node.children:
            _render_node(child, parts, in_section=sec)

    elif isinstance(node, SubsectionNode):
        parts.append(f'  <p><em>{esc(node.heading)}</em></p>')
        for child in node.children:
            _render_node(child, parts, in_section=in_section)

    elif isinstance(node, SubSubsectionNode):
        parts.append(f'  <p><strong><em>{esc(node.heading)}</em></strong></p>')
        for child in node.children:
            _render_node(child, parts, in_section=in_section)

    elif isinstance(node, TableNode):
        parts.append(node.html)

    elif isinstance(node, FootnoteNode):
        parts.append(
            f'  <p><small><em>{esc(node.text)}</em></small></p>')

    elif isinstance(node, FooterNode):
        parts.append(
            f'  <p><small><em>{esc(node.text)}</em></small></p>')

    elif isinstance(node, TOCNode):
        parts.append('  <p><strong>Table of Contents</strong></p>')
        parts.append('  <ul style="list-style:none;padding-left:20px">')
        for title, pg in node.entries:
            pg_str = f' <span style="float:right">{esc(pg)}</span>' if pg else ''
            parts.append(f'   <li>{esc(title)}{pg_str}</li>')
        parts.append('  </ul>')

    elif isinstance(node, RelatedResourcesNode):
        parts.append('  <p><strong>Related Coverage Resources</strong></p>')
        parts.append('  <ul style="list-style:none;padding-left:20px">')
        for entry in node.entries:
            if isinstance(entry, tuple) and len(entry) == 2:
                text, url = entry
                if url:
                    parts.append(
                        f'   <li><a href="{esc(url)}">{esc(text)}</a></li>')
                else:
                    parts.append(f'   <li>{esc(text)}</li>')
            else:
                # Backward compatibility: plain string entry
                parts.append(f'   <li>{esc(entry)}</li>')
        parts.append('  </ul>')


def _render_header(node: HeaderNode, parts: list[str]) -> None:
    meta      = node.meta
    title     = meta.get('title', '')
    policy_id = meta.get('policy_id', '')
    pub_date  = meta.get('publish_date', '')
    doc_type  = meta.get('doc_type', 'Coverage Policy')
    source    = meta.get('source_url', '')

    parts.append(f"""<!doctype html>
<html>
 <head>
  <title>{esc(policy_id)} {esc(title)}</title>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
  <meta name="source_url" content="{esc(source)}">
 </head>
 <body>
  <h1 style="text-align:center;font-size:22pt">{esc(doc_type)}</h1>
  <p>&nbsp;</p>
  <table id="docDetails">
   <tbody>
    <tr><td colspan="2"><strong>Policy Title:</strong> {esc(title)}</td></tr>
    <tr>
     <td><strong>Coverage Policy Number:</strong> {esc(policy_id)}</td>
     <td><strong>Effective Date:</strong> {esc(pub_date)}</td>
    </tr>
   </tbody>
  </table>
  <p>&nbsp;</p>
  <hr>
  <p>&nbsp;</p>""")


# ════════════════════════════════════════════════════════════════════════════
# Footer helper
# ════════════════════════════════════════════════════════════════════════════

def _insert_footer_hr(parts: list[str]) -> None:
    hr_idx = None
    for i in range(len(parts) - 1, -1, -1):
        if '<small><em>' in parts[i] and (
                'Cigna Companies' in parts[i] or
                '© 20' in parts[i] or
                'operating subsidiaries' in parts[i]):
            hr_idx = i
    if hr_idx is not None:
        parts.insert(hr_idx, '  <hr>')
    else:
        parts.append('  <hr>')


# ════════════════════════════════════════════════════════════════════════════
# Main emit
# ════════════════════════════════════════════════════════════════════════════

def emit(doc: CignaDoc) -> str:
    parts: list[str] = []

    for node in doc.nodes:
        _render_node(node, parts)

    _insert_footer_hr(parts)
    parts.append(' </body>\n</html>')

    return '\n'.join(parts)


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse, sys
    from cigna_parse import parse

    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf', required=True)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    doc  = parse(Path(args.pdf).expanduser())
    html = emit(doc)

    if args.out:
        Path(args.out).write_text(html, encoding='utf-8')
        print(f"Written: {args.out}", file=sys.stderr)
    else:
        print(html)
