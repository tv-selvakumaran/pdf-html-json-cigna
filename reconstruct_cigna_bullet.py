#!/usr/bin/env python3
"""
reconstruct_cigna_bullet.py  —  V3
====================================
Reconstructs bullet/numbered/note hierarchy from pre-extracted paragraph lines.

Public API:
    reconstruct(lines)   -> ParagraphBlock
    print_block(block)
    print_raw_lines(lines)
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional

from extractor import extract_paragraph_lines, BULLET_CHARS   # noqa: F401


# ════════════════════════════════════════════════════════════════════════════
# Output node types
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PlainText:
    text: str
    kind: str = 'plain'

@dataclass
class SubSubItem:
    text: str
    kind: str = 'sub_sub'

@dataclass
class SubBulletItem:
    text: str
    children: list = field(default_factory=list)   # list[SubSubItem]
    kind: str = 'sub_bullet'

@dataclass
class BulletItem:
    text: str
    children: list = field(default_factory=list)   # list[SubBulletItem]
    kind: str = 'bullet'

@dataclass
class RomanItem:
    text: str
    children: list = field(default_factory=list)   # list[NoteItem]
    kind: str = 'roman'

@dataclass
class LetterItem:
    text: str
    children: list = field(default_factory=list)   # list[RomanItem]
    kind: str = 'letter'

@dataclass
class NoteItem:
    text: str
    kind: str = 'note'

@dataclass
class NumItem:
    text: str
    children: list = field(default_factory=list)   # list[LetterItem]
    kind: str = 'num'

@dataclass
class ParagraphBlock:
    plain_text: str = ''
    items: list = field(default_factory=list)
    kind: str = 'paragraph_block'


# ════════════════════════════════════════════════════════════════════════════
# Marker detection helpers
# ════════════════════════════════════════════════════════════════════════════

def _is_bullet_marker(l: dict) -> bool:
    return 'SymbolMT' in l['fontname'] and l['text'].strip() in BULLET_CHARS

def _is_bold_bullet(l: dict) -> bool:
    txt = l['text']
    starts_with_bullet = (
        txt.startswith('•') or txt.startswith('\u2022') or
        (txt and ord(txt[0]) in (0xf0b7, 0xf0a7, 0x2023))
    )
    return (l['x0'] <= 80 and starts_with_bullet and
            'SymbolMT'  not in l['fontname'] and
            'Wingdings' not in l['fontname'])

def _is_sub_marker(l: dict) -> bool:
    return 'Courier' in l['fontname'] and l['text'].strip() == 'o'

def _is_subsub_marker(l: dict) -> bool:
    return 'Wingdings' in l['fontname']

def _is_any_marker(l: dict) -> bool:
    return (_is_bullet_marker(l) or _is_bold_bullet(l) or
            _is_sub_marker(l) or _is_subsub_marker(l))

def _is_num(l: dict) -> bool:
    return l['x0'] <= 65 and bool(re.match(r'^\d+\.', l['text']))

def _is_roman(l: dict) -> bool:
    """Roman numeral sub-items: i. ii. iii. etc. at x=85-100"""
    return (85 <= l['x0'] <= 100 and
            bool(re.match(r'^[ivxIVX]+\.\s', l['text'])))

def _is_letter(l: dict) -> bool:
    return 68 <= l['x0'] <= 78 and bool(re.match(r'^[A-E]\)', l['text']))

def _is_note(l: dict) -> bool:
    return l['text'].startswith('Note:') or l['text'].startswith('Note :')

def _clean(t: str) -> str:
    return re.sub(r'\s+', ' ', t).strip()


# ════════════════════════════════════════════════════════════════════════════
# Main reconstruction
# ════════════════════════════════════════════════════════════════════════════

def reconstruct(lines: list[dict]) -> ParagraphBlock:
    """
    Build a ParagraphBlock from pre-extracted paragraph lines.
    Pass 1: resolve markers to adjacent text.
    Pass 2: build typed hierarchy.
    """
    if not lines:
        return ParagraphBlock()

    sl = sorted(lines, key=lambda l: (l.get('_page', 0), l['top']))
    n  = len(sl)
    claimed      = set()
    pre_resolved = set()
    entries      = []      # (y, etype, text, x0)

    # ── Pass 1 ───────────────────────────────────────────────────────────
    for i, line in enumerate(sl):
        y = line['top']

        mtype_tag = line.get('marker_type')
        if mtype_tag:
            text_val = line['text']
            x0_val   = line['x0']
            if not text_val:
                order = [i - 1, i + 1] if mtype_tag == 'sub_bullet' else [i + 1, i - 1]
                for j in order:
                    if 0 <= j < n and j not in claimed and j not in pre_resolved:
                        cand = sl[j]
                        if abs(cand['top'] - y) <= 8 and not cand.get('marker_type'):
                            text_val = cand['text']
                            # x0_val   = cand['x0']  <-- BUG: takes text x0
                            claimed.add(j)
                            break
            # NEW: if text is just a trademark/registered symbol,
            # prepend it to the next line's text
            if text_val.strip() in ('®', '™', '\u00ae', '\u2122'):
                for j in [i + 1]:
                    if (0 <= j < n and j not in claimed and
                            j not in pre_resolved and
                            not sl[j].get('marker_type')):
                        if abs(sl[j]['top'] - y) <= 15:
                            text_val = text_val.strip() + sl[j]['text']
                            claimed.add(j)
                            break
            entries.append((line.get('_page', 0), y, mtype_tag, text_val, x0_val))
            pre_resolved.add(i)
            continue

        if _is_bullet_marker(line):
            for j in [i + 1, i - 1]:
                if 0 <= j < n and j not in claimed and not _is_any_marker(sl[j]):
                    if abs(sl[j]['top'] - y) <= 8:
                        claimed.add(j)
                        entries.append((line.get('_page',0), y, 'bullet', sl[j]['text'], line['x0']))
                        break
            else:
                entries.append((line.get('_page',0), y, 'bullet', '', line['x0']))

        elif _is_bold_bullet(line):
            own = line['text'].lstrip('•\u2022 ').strip()
            # If own is just a trademark/registered symbol or empty,
            # the real bullet text is on the next line
            if own in ('®', '™', '\u00ae', '\u2122', ''):
                for j in [i + 1]:
                    if (0 <= j < n and
                            j not in claimed and
                            j not in pre_resolved and
                            not _is_any_marker(sl[j])):
                        nxt = sl[j]
                        if abs(nxt['top'] - y) <= 15:
                            own = (own + nxt['text']).strip()
                            claimed.add(j)
                            break
            entries.append((line.get('_page',0), y, 'bullet', own, line['x0']))

        elif _is_sub_marker(line):
            found = False
            for j in [i - 1, i + 1]:
                if 0 <= j < n and j not in claimed and not _is_any_marker(sl[j]):
                    if abs(sl[j]['top'] - y) <= 8:
                        claimed.add(j)
                        entries.append((line.get('_page',0), y, 'sub', sl[j]['text'], sl[j]['x0']))
                        found = True
                        break
            if not found:
                entries.append((line.get('_page',0), y, 'sub', '', line['x0']))

        elif _is_subsub_marker(line):
            found = False
            for j in [i + 1, i - 1]:
                if 0 <= j < n and j not in claimed and not _is_any_marker(sl[j]):
                    if abs(sl[j]['top'] - y) <= 8:
                        claimed.add(j)
                        entries.append((line.get('_page',0), y, 'subsub', sl[j]['text'], sl[j]['x0']))
                        found = True
                        break
            if not found:
                entries.append((line.get('_page',0), y, 'subsub', '', line['x0']))

        elif _is_note(line):
            # Collect all continuation lines belonging to this note
            # before creating the entry — prevents fragmentation
            note_text = line['text']
            last_was_num = False
            j = i + 1
            while j < len(sl):
                if j in claimed or j in pre_resolved:
                    j += 1
                    continue
                nxt = sl[j]
                # Stop at any new structural element
                if (_is_any_marker(nxt) or
                        _is_num(nxt) or _is_note(nxt) or
                        _is_roman(nxt) or _is_letter(nxt) or
                        nxt.get('bold') or
                        nxt.get('size', 0) >= 12.0 or
                        nxt.get('in_table')):
                    break
                nxt_text = nxt['text'].strip()
                # Numbered item within note — separate with <br>
                if re.match(r'^\d+[)]', nxt_text):
                    note_text = note_text + '<br>' + nxt_text
                    last_was_num = True
                else:
                    # Plain continuation — append with space
                    # If continuing a numbered item, keep with that item
                    if last_was_num:
                        note_text = _clean(note_text + ' ' + nxt_text)
                    else:
                        note_text = _clean(note_text + ' ' + nxt_text)
                    last_was_num = False
                claimed.add(j)
                j += 1
            entries.append((line.get('_page', 0), y, 'note',
                            note_text, line['x0']))

        elif _is_num(line):
            entries.append((line.get('_page',0), y, 'num', line['text'], line['x0']))

        elif _is_note(line):
            entries.append((line.get('_page',0), y, 'note', line['text'], line['x0']))

        elif _is_roman(line):
            entries.append((line.get('_page',0), y, 'roman', line['text'], line['x0']))

        elif _is_letter(line):
            entries.append((line.get('_page',0), y, 'letter', line['text'], line['x0']))

    # ── Collect unclaimed plain/continuation lines ────────────────────────
    for i, line in enumerate(sl):
        if i in pre_resolved or i in claimed:
            continue
        if (not _is_any_marker(line) and not line.get('marker_type') and
                not _is_num(line) and not _is_note(line) and
                not _is_roman(line) and not _is_letter(line)):
            entries.append((line.get('_page',0), line['top'], 'plain', line['text'], line['x0']))

    entries.sort(key=lambda e: (e[0], e[1]))  # sort by (page, y)

    # ── Pass 2 ───────────────────────────────────────────────────────────
    block          = ParagraphBlock()
    current_bullet : Optional[BulletItem]    = None
    current_sub    : Optional[SubBulletItem] = None
    current_num    : Optional[NumItem]       = None
    current_letter : Optional[LetterItem]    = None
    current_roman  : Optional[RomanItem]     = None
    current_note   : Optional[NoteItem]      = None
    current_bullet_x0: float = 0.0
    current_bullet_pg: int = 0

    for _pg, y, etype, text, x0 in entries:
        text = _clean(text)

        if etype == 'bullet':
            current_bullet = BulletItem(text=text)
            current_bullet_x0 = x0 
            current_bullet_pg = _pg
            current_sub    = None
            current_num    = None
            current_letter = None
            current_roman  = None
            current_note   = None
            block.items.append(current_bullet)

        elif etype in ('sub', 'sub_bullet'):
            current_roman  = None
            current_note   = None
            if current_bullet is None:
                block.items.append(PlainText(text=text))
            else:
                current_sub = SubBulletItem(text=text)
                current_bullet.children.append(current_sub)

        elif etype in ('subsub', 'sub_sub_bullet'):
            current_roman = None
            current_note  = None
            if current_sub is not None:
                current_sub.children.append(SubSubItem(text=text))
            elif current_bullet is not None:
                sb = SubBulletItem(text=text)
                current_bullet.children.append(sb)
            else:
                block.items.append(PlainText(text=text))

        elif etype == 'num':
            current_num    = NumItem(text=text)
            current_bullet = None
            current_sub    = None
            current_letter = None
            current_roman  = None
            current_note   = None
            block.items.append(current_num)

        elif etype == 'letter':
            current_letter = LetterItem(text=text)
            current_roman  = None
            current_note   = None
            if current_num is not None:
                current_num.children.append(current_letter)
            else:
                block.items.append(PlainText(text=text))

        elif etype == 'roman':
            current_roman = RomanItem(text=text)
            current_note  = None
            if current_letter is not None:
                current_letter.children.append(current_roman)
            elif current_num is not None:
                # Orphan roman — attach to last letter child or create stub
                if current_num.children:
                    current_num.children[-1].children.append(current_roman)
                else:
                    block.items.append(PlainText(text=text))
            else:
                block.items.append(PlainText(text=text))

        elif etype == 'note':
            current_note = NoteItem(text=text)
            if current_roman is not None:
                # Note nested inside roman item
                current_note = NoteItem(text=text)
                current_roman.children.append(current_note)
                current_roman = None  # clear so continuations go to note
            elif current_num is not None or current_letter is not None:
                # Note nested inside num/letter context — preserve NoteItem
                current_note = NoteItem(text=text)
                block.items.append(current_note)
            else:
                # Standalone Note with no parent — treat as PlainText
                # continuations will be picked up by the plain continuation handler
                block.items.append(PlainText(text=text))

        else:  # plain continuation
            if not text:
                continue
            if current_roman is not None:
                # Continuation at x=108 belongs to roman item
                current_roman.text = _clean(current_roman.text + ' ' + text)
            elif current_note is not None:
                current_note.text = _clean(current_note.text + ' ' + text)
            elif current_sub is not None:
                current_sub.text = (_clean(current_sub.text + ' ' + text)
                                    if current_sub.text else text)
            elif current_bullet is not None:
                # New paragraph if: different page AND x0 is at or left of bullet marker x0
                crosses_page = (_pg != current_bullet_pg)
                if crosses_page and x0 < current_bullet_x0 + 0.5:
                    current_bullet = None
                    current_sub = None
                    block.items.append(PlainText(text=text))
                elif not crosses_page and x0 < current_bullet_x0 - 2:
                    current_bullet = None
                    current_sub = None
                    block.items.append(PlainText(text=text))
                else:
                    current_bullet.text = (_clean(current_bullet.text + ' ' + text)
                                           if current_bullet.text else text) 
            elif current_num is not None:
                if current_num.children:
                    li = current_num.children[-1]
                    li.text = _clean(li.text + ' ' + text) if li.text else text
                else:
                    current_num.text = _clean(current_num.text + ' ' + text)
            else:
                block.items.append(PlainText(text=text))

    # Collapse to plain_text if every item is PlainText
    if all(isinstance(it, PlainText) for it in block.items):
        block.plain_text = _clean(
            ' '.join(it.text for it in block.items if it.text))
        block.items = []

    return block


# ════════════════════════════════════════════════════════════════════════════
# Debug helpers
# ════════════════════════════════════════════════════════════════════════════

def print_block(block: ParagraphBlock, indent: int = 0) -> None:
    pad = '  ' * indent
    print(f"{pad}[PARA]")
    if block.plain_text:
        print(f"{pad}  [TEXT] {block.plain_text}")
    for item in block.items:
        if isinstance(item, BulletItem):
            print(f"{pad}  [•] {item.text}")
            for sub in item.children:
                print(f"{pad}    [○] {sub.text}")
                for ssub in sub.children:
                    print(f"{pad}      [▪] {ssub.text}")
        elif isinstance(item, NumItem):
            print(f"{pad}  [NUM] {item.text}")
            for li in item.children:
                print(f"{pad}    [LETTER] {li.text}")
                for ri in li.children:
                    print(f"{pad}      [ROMAN] {ri.text}")
                    for ni in ri.children:
                        print(f"{pad}        [NOTE] {ni.text}")
        elif isinstance(item, NoteItem):
            print(f"{pad}  [NOTE] {item.text}")
        elif isinstance(item, PlainText):
            print(f"{pad}  [TEXT] {item.text}")


def print_raw_lines(lines: list[dict]) -> None:
    for l in lines:
        mt = l.get('marker_type', '')
        print(f"y={l['top']:5.1f} x={l['x0']:5.1f} "
              f"mt={mt:<14} fn={l['fontname'][:22]:<22} | {l['text'][:70]}")
