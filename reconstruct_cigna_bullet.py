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

from cigna_extractor import extract_paragraph_lines, BULLET_CHARS   # noqa: F401


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
    notes: list = field(default_factory=list)        # list[NoteItem]
    kind: str = 'roman'

@dataclass
class LetterItem:
    text: str
    children: list = field(default_factory=list)   # list[RomanItem]
    notes: list = field(default_factory=list)       # list[NoteItem]
    kind: str = 'letter'

@dataclass
class ParenNumItem:
    text: str
    children: list = field(default_factory=list)
    notes: list = field(default_factory=list)   
    kind: str = 'paren_num'

@dataclass
class NoteItem:
    text: str
    kind: str = 'note'

@dataclass
class NumItem:
    text: str
    children: list = field(default_factory=list)   # list[LetterItem]
    notes: list = field(default_factory=list)       # list[NoteItem]
    epilogue: list = field(default_factory=list) 
    kind: str = 'num'
    number: int | None = None

@dataclass
class ParagraphBlock:
    plain_text: str = ''
    items: list = field(default_factory=list)
    kind: str = 'paragraph_block'

@dataclass
class EpilogueSection:
    heading: str
    content: str = ''
    kind: str = 'epilogue'


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
    if l['x0'] > 75:
        return False
    _m = re.match(r'^(\d+)[.)]', l['text'])
    if not _m:
        return False
    _digits = _m.group(1)
    # Exclude 4-digit sequences (years, e.g. "2022)") -- a genuine
    # numbered-list marker is realistically 1-3 digits; a 4-digit
    # number immediately followed by '.' or ')' is virtually always a
    # year appearing parenthetically in prose, not a list item.
    if len(_digits) >= 4:
        return False
    return True

def _is_nested_num_fallback(l: dict) -> bool:
    """Interim fallback: a numbered marker (1. 2. 3.) at ANY indentation,
    used only when nothing else in the marker chain matched. This covers
    numbered sub-lists nested deeper than the fixed x0 bands anticipate
    (e.g. num -> letter -> roman -> num), without touching the existing
    absolute-x0 detectors and their tuned ranges."""
    return bool(re.match(r'^\d+[.)]\s', l['text']))

def _is_letter(l: dict) -> bool:
    if not (60 <= l['x0'] <= 115):
        return False
    _m = re.match(r'^([A-Z])[.)]', l['text'])
    if not _m:
        return False
    _after_marker = l['text'][_m.end():].strip()
    # A genuine lettered item always has real content following the
    # marker on the same line. A bare marker with nothing (or only a
    # colon/punctuation) after it -- e.g. "D):" -- is a tail fragment
    # of a preceding sentence listing upcoming sub-items, not an
    # actual lettered item.
    if not _after_marker or not _after_marker[0].isalnum():
        return False
    return True


def _is_roman_old(l: dict) -> bool:
    """Roman numeral sub-items: i. ii. iii. etc. at x=84-128"""
    return (84 <= l['x0'] <= 156 and
            bool(re.match(r'^[ivxIVX]+[.)]\s', l['text'])))

def _is_roman(l: dict, is_cpg: bool = False) -> bool:
    """Roman numeral sub-items: i. ii. iii. etc. at x=84-128 (nested),
    or x=54-84 for cpg-family documents, which use top-level roman
    numeral lists with a narrower left margin than other families."""
    _x0_ok = (84 <= l['x0'] <= 156) or (is_cpg and 54 <= l['x0'] <= 84)
    return (_x0_ok and
            bool(re.match(r'^[ivxIVX]+[.)]\s', l['text'])))


def _is_paren_num(l: dict) -> bool:
    """Parenthesized numeral sub-items: (1) (2) etc. — a 4th-tier marker
    nested under roman items, e.g. num -> letter -> roman -> paren_num."""
    _m = re.match(r'^\((\d+)\)', l['text'])
    if not _m:
        return False
    if len(_m.group(1)) >= 4:
        return False  # excludes years like (2024)
    return True

def _is_note(l: dict) -> bool:
    return l['text'].startswith('Note:') or l['text'].startswith('Note :')

def _clean(t: str) -> str:
    return re.sub(r'\s+', ' ', t).strip()


# ════════════════════════════════════════════════════════════════════════════
# Main reconstruction
# ════════════════════════════════════════════════════════════════════════════


def _resolve_entries(lines: list[dict], is_cpg: bool = False) -> list[tuple]:
    """
    Pass 1: resolve markers to adjacent text.
    Returns sorted list of (page, y, etype, text, x0, size) tuples.
    """

    from cigna_constants import FOOTER_PATTERNS
    # Filter page footer lines before processing
    lines = [l for l in lines 
             if not any(p.match(l['text'].strip()) for p in FOOTER_PATTERNS)]
    if not lines:
        return []

    sl = sorted(lines, key=lambda l: (l.get('_page', 0), l['top']))
    n  = len(sl)
    claimed      = set()
    pre_resolved = set()
    entries      = []

    # ── Pass 1 ───────────────────────────────────────────────────────────
    for i, line in enumerate(sl):
        if i in claimed or i in pre_resolved:
            continue
        y = line['top']
        size = line.get('size', 10.0) # ← add this once
        bold = line.get('bold', False)
        underline = line.get('underline', False)

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
            entries.append((line.get('_page', 0), y, mtype_tag, text_val, x0_val, size, bold, underline, 
                            line.get('leading_bold', False), line.get('italic', False)))
            pre_resolved.add(i)
            continue

        if _is_bullet_marker(line):
            for j in [i + 1, i - 1]:
                if 0 <= j < n and j not in claimed and not _is_any_marker(sl[j]):
                    if abs(sl[j]['top'] - y) <= 8:
                        claimed.add(j)
                        entries.append((line.get('_page',0), y, 'bullet', sl[j]['text'], 
                                        line['x0'], size, bold, underline, 
                                        line.get('leading_bold', False), line.get('italic', False)))
                        break
            else:
                entries.append((line.get('_page',0), y, 'bullet', '', line['x0'], size, 
                                bold, underline, line.get('leading_bold', False), line.get('italic', False)))

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
            entries.append((line.get('_page',0), y, 'bullet', own, line['x0'], size, bold, underline, 
                            line.get('leading_bold', False), line.get('italic', False)))

        elif _is_sub_marker(line):
            found = False
            for j in [i - 1, i + 1]:
                if 0 <= j < n and j not in claimed and not _is_any_marker(sl[j]):
                    if abs(sl[j]['top'] - y) <= 8:
                        claimed.add(j)
                        entries.append((line.get('_page',0), y, 'sub', 
                                        sl[j]['text'], sl[j]['x0'], size, bold, underline, 
                                        line.get('leading_bold', False), line.get('italic', False)))
                        found = True
                        break
            if not found:
                entries.append((line.get('_page',0), y, 'sub', '', line['x0'], size, bold, underline, 
                                line.get('leading_bold', False), line.get('italic', False)))

        elif _is_subsub_marker(line):
            found = False
            for j in [i + 1, i - 1]:
                if 0 <= j < n and j not in claimed and not _is_any_marker(sl[j]):
                    if abs(sl[j]['top'] - y) <= 8:
                        claimed.add(j)
                        entries.append((line.get('_page',0), y, 'subsub', sl[j]['text'], 
                                        sl[j]['x0'], size, bold, underline, 
                                        line.get('leading_bold', False), line.get('italic', False)))
                        found = True
                        break
            if not found:
                entries.append((line.get('_page',0), y, 'subsub', '', line['x0'], size, bold, underline, 
                                line.get('leading_bold', False), line.get('italic', False)))

        elif _is_note(line):
            # Collect all continuation lines belonging to this note
            # before creating the entry — prevents fragmentation
            note_text = line['text']
            note_x0 = line['x0']
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
                        _is_paren_num(nxt) or 
                        nxt.get('leading_bold') or
                        nxt.get('size', 0) >= 12.0 or
                        nxt.get('in_table') or 
                        nxt.get('x0', note_x0) < note_x0 - 2):
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
            entries.append((line.get('_page', 0), y, 'note', note_text, line['x0'], size, bold, 
                            underline, line.get('leading_bold', False), line.get('italic', False)))

        elif _is_num(line):
            entries.append((line.get('_page',0), y, 'num', line['text'], line['x0'], size, bold, 
                            underline, line.get('leading_bold', False), line.get('italic', False)))

        elif _is_roman(line, is_cpg=is_cpg):
            entries.append((line.get('_page',0), y, 'roman', line['text'], line['x0'], size, bold, 
                            underline, line.get('leading_bold', False), line.get('italic', False)))

        elif _is_letter(line):
            _letter_match = re.match(r'^([A-Z])[.)]', line['text'].strip())
            _is_ambiguous_i = bool(_letter_match and _letter_match.group(1) == 'I')
            _etype = 'letter'
            if _is_ambiguous_i:
                # Peek ahead for a sibling at the same x0: if it looks like
                # "II."/"III."/"IV." (roman continuation) rather than "B."
                # (letter continuation), this "I." is a top-level roman
                # numeral, not a nested letter marker.
                for j in range(i + 1, len(sl)):
                    if j in claimed or j in pre_resolved:
                        continue
                    _nxt = sl[j]
                    if abs(_nxt['x0'] - line['x0']) > 3.0:
                        continue
                    _nxt_text = _nxt['text'].strip()
                    if re.match(r'^(II|III|IV|V)[.)]', _nxt_text):
                        _etype = 'roman'
                    break
            entries.append((line.get('_page', 0), y, _etype, line['text'], line['x0'], size, bold,
                            underline, line.get('leading_bold', False), line.get('italic', False)))

        elif _is_paren_num(line):
            entries.append((line.get('_page',0), y, 'paren_num', line['text'],
                             line['x0'], size, bold, underline, 
                             line.get('leading_bold', False), line.get('italic', False)))
        elif _is_nested_num_fallback(line):
            entries.append((line.get('_page',0), y, 'num', line['text'],
                             line['x0'], size, bold, underline, 
                             line.get('leading_bold', False), line.get('italic', False)))

    # ── Collect unclaimed plain/continuation lines ────────────────────────
    for i, line in enumerate(sl):
        if i in pre_resolved or i in claimed:
            continue
        if (not _is_any_marker(line) and not line.get('marker_type') and
                not _is_num(line) and not _is_note(line) and
                not _is_roman(line, is_cpg=is_cpg) and not _is_letter(line) and
                not _is_paren_num(line)):
            _size         = line.get('size', 10.0)
            _bold         = line.get('bold', False)
            _underline    = line.get('underline', False)
            _leading_bold = line.get('leading_bold', False)
            entries.append((line.get('_page',0), line['top'], 'plain', 
                            line['text'], line['x0'], _size, _bold, _underline, 
                            _leading_bold, line.get('italic', False)))

    entries.sort(key=lambda e: (e[0], e[1]))  # sort by (page, y)
    return entries


def _build_block(entries: list[tuple], citation_numbers: dict = None) -> ParagraphBlock:
    """
    Pass 2: build typed hierarchy from entries.
    Returns ParagraphBlock.
    """
    citation_numbers = citation_numbers or {}

    # ── Pass 2 ───────────────────────────────────────────────────────────
    block          = ParagraphBlock()
    current_bullet          : Optional[BulletItem]      = None
    current_sub             : Optional[SubBulletItem]   = None
    current_num             : Optional[NumItem]         = None
    current_num_x0          : float                     = 0.0
    current_num_pg          : int                       = 0
    current_letter          : Optional[LetterItem]      = None
    current_roman           : Optional[RomanItem]       = None
    current_paren_num       : Optional[ParenNumItem]    = None
    current_note            : Optional[NoteItem]        = None
    current_note_x0         : float                     = 0.0
    current_note_pg         : int                       = 0
    current_epilogue        : Optional[EpilogueSection] = None
    current_letter_child_x0 : Optional[float]           = None
    current_bullet_x0       : float                     = 0.0
    current_bullet_pg       : int                       = 0

    for _pg, y, etype, text, x0, size, bold, underline, leading_bold, italic in entries:
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
            from reconstruct_cigna_bullet import SubBulletItem as _SBI
            if current_sub is not None and not isinstance(current_sub, SubBulletItem):
                # current_sub is a real SubItem — append SubSubItem as child
                current_sub.children.append(SubSubItem(text=text))
            elif current_bullet is not None:
                sb = SubBulletItem(text=text)
                current_bullet.children.append(sb)
                current_sub = sb
            else:
                block.items.append(PlainText(text=text))

        elif etype == 'num':
            _num_value = citation_numbers.get((_pg, y))
            current_num_x0          = x0
            current_num_pg          = _pg
            current_bullet          = None
            _is_nested_under_roman = (
                current_roman is not None and 
                (x0 > current_letter_child_x0 - 3 if current_letter_child_x0 else False))
            current_letter_child_x0 = None
            if _is_nested_under_roman:
                nested = ParenNumItem(text=text)  
                current_roman.children.append(nested)
                current_paren_num = nested
                current_num = NumItem(text=text, number=_num_value)
            else:
                current_num             = NumItem(text=text, number=_num_value)
                current_bullet          = None
                current_sub             = None
                current_letter          = None
                current_roman           = None
                current_note            = None
                current_epilogue        = None
                current_paren_num       = None
                block.items.append(current_num)

        elif etype == 'letter':
            current_letter          = LetterItem(text=text)
            current_roman           = None
            current_note            = None
            current_epilogue        = None
            current_letter_child_x0 = None
            current_paren_num       = None
            if current_num is not None:
                current_num.children.append(current_letter)
            else:
                block.items.append(PlainText(text=text))

        elif etype == 'roman':
            current_roman           = RomanItem(text=text)
            current_note            = None
            current_epilogue        = None
            current_paren_num       = None
            current_letter_child_x0 = x0
            if current_letter is not None:
                current_letter.children.append(current_roman)
            elif current_num is not None:
                # Orphan roman — attach to last letter child or create stub
                if current_num.children:
                    current_num.children[-1].children.append(current_roman)
                else:
                    block.items.append(current_roman)
            else:
                block.items.append(current_roman)

        elif etype == 'paren_num':
            current_note     = None
            current_epilogue = None
            node = ParenNumItem(text=text)
            if current_roman is not None:
                current_roman.children.append(node)
            elif current_letter is not None:
                current_letter.children.append(node)
            else:
                block.items.append(node)
            current_paren_num = node

        elif etype == 'note':
            if current_paren_num is not None:
                current_note = NoteItem(text=text)
                current_note_x0 = x0
                current_note_pg = _pg
                current_paren_num.notes.append(current_note)
            elif current_roman is not None:
                # Note nested inside roman item
                current_note = NoteItem(text=text)
                current_note_x0 = x0
                current_note_pg = _pg
                current_roman.notes.append(current_note)
            elif current_letter is not None:
                # Note nested inside letter context — preserve NoteItem
                current_note = NoteItem(text=text)
                current_note_x0 = x0
                current_note_pg = _pg
                current_letter.notes.append(current_note)
            elif current_num is not None:
                # Note nested inside num context — preserve NoteItem
                current_note = NoteItem(text=text)
                current_note_x0 = x0
                current_note_pg = _pg
                current_num.notes.append(current_note)
            else:
                # Standalone Note with no parent — treat as PlainText
                # continuations will be picked up by the plain continuation handler
                block.items.append(PlainText(text=text))

        else:  # plain continuation
            if not text:
                continue
            _EPILOGUE_HEAD_RE = re.compile(r'^([A-Z][A-Za-z]{2,20})\s*\.\s+(\S.*)$')
            if current_num is not None:
                m = _EPILOGUE_HEAD_RE.match(text.strip())
                if m and leading_bold and (current_letter_child_x0 is None or
                          abs(x0 - current_letter_child_x0) <= 3.0):
                    # New epilogue heading (e.g. "Dosing.") — belongs to the num item
                    # as a whole, regardless of which letter branch (A./B.) was taken.
                    current_letter    = None
                    current_roman     = None
                    current_paren_num = None
                    current_note      = None
                    heading = m.group(1) + '.'
                    rest = m.group(2)
                    current_epilogue  = EpilogueSection(heading=heading, content=rest)
                    current_num.epilogue.append(current_epilogue)
                    continue
            if current_epilogue is not None:
                current_epilogue.content = (
                    _clean(current_epilogue.content + ' ' + text)
                    if current_epilogue.content else text)
            elif current_paren_num is not None:
                current_paren_num.text = _clean(current_paren_num.text + ' ' + text)
            elif current_note is not None:
                crosses_page = (_pg != current_note_pg)
                _prev_lacks_terminal_punct = bool(
                    current_note.text and current_note.text.rstrip() and
                    current_note.text.rstrip()[-1] not in '.:;?!')
                _current_starts_lowercase = bool(text and text.strip() and text.strip()[0].islower())
                _looks_like_continuation = (crosses_page and
                                             _prev_lacks_terminal_punct and
                                             _current_starts_lowercase)

                if crosses_page and not _looks_like_continuation and x0 < current_note_x0 + 0.5:
                    current_note = None
                    block.items.append(PlainText(text=text))
                elif not crosses_page and x0 < current_note_x0 - 2:
                    current_note = None
                    block.items.append(PlainText(text=text))
                else:
                    current_note.text = _clean(current_note.text + ' ' + text)
            elif current_roman is not None:
                current_roman.text = _clean(current_roman.text + ' ' + text)
            elif current_sub is not None:
                current_sub.text = (_clean(current_sub.text + ' ' + text)
                                    if current_sub.text else text)
            elif current_bullet is not None:
                # New paragraph if: different page AND x0 is at or left of bullet marker x0
                crosses_page = (_pg != current_bullet_pg)
                _prev_lacks_terminal_punct = bool(
                    current_bullet.text and current_bullet.text.rstrip() and
                    current_bullet.text.rstrip()[-1] not in '.:;?!')
                _current_starts_lowercase = bool(text and text.strip() and text.strip()[0].islower())
                _looks_like_continuation = (crosses_page and
                                             _prev_lacks_terminal_punct and
                                             _current_starts_lowercase)
                if crosses_page and not _looks_like_continuation and x0 < current_bullet_x0 + 0.5:
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
                crosses_page = (_pg != current_num_pg)
                if crosses_page and x0 < current_num_x0 + 0.5:
                    current_num = None
                    current_letter = None
                    block.items.append(PlainText(text=text))
                elif not crosses_page and (x0 < current_num_x0 - 2 or 
                                            (bold and x0 <= current_num_x0 + 2)): 
                    current_num = None
                    current_letter = None
                    block.items.append(PlainText(text=text))
                elif current_num.children:
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


def reconstruct(lines: list[dict]) -> ParagraphBlock:
    """Convenience wrapper — calls _resolve_entries then _build_block."""
    if not lines:
        return ParagraphBlock()
    entries = _resolve_entries(lines)
    return _build_block(entries)


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
