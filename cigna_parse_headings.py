#!/usr/bin/env python3
"""
cigna_parse_headings.py
=======================
Section and subsection heading classifiers for the Cigna V5 converter.
"""

from __future__ import annotations
import re
from typing import Optional
from cigna_parse_nodes import SECTION_VOCAB, SUBSECTION_VOCAB, BOILERPLATE_FRAGMENTS
from extractor import _has_underline_rect


def normalize_heading(text: str) -> str:
    """Remove letter-spacing artifacts: 'P OLICY S TATEMENT' → 'policystatement'."""
    stripped = text.strip()
    no_spaces = stripped.replace(' ', '')
    if no_spaces.isupper() and len(no_spaces) >= 4:
        return no_spaces.lower()
    t = text
    for _ in range(5):
        t2 = re.sub(r'(?<=[A-Z]) (?=[A-Z])', '', t)
        if t2 == t:
            break
        t = t2
    return t.lower().strip()


def _is_garbled_match(garbled: str, target: str) -> bool:
    """Check if garbled is target with up to 3 chars deleted (letter-drop artifact)."""
    if garbled == target:
        return True
    if len(garbled) > len(target):
        return False
    gi, ti, skipped = 0, 0, 0
    while gi < len(garbled) and ti < len(target):
        if garbled[gi] == target[ti]:
            gi += 1
        else:
            skipped += 1
        ti += 1
    skipped += len(target) - ti
    return gi == len(garbled) and skipped <= 3


def classify_section(line: dict,
                     vocab: set = None) -> Optional[str]:
    if vocab is None:
        from cigna_parse_nodes import SECTION_VOCAB
        vocab = SECTION_VOCAB
    if not line['bold']:
        return None
    text = line['text']
    norm = normalize_heading(text)

    if line['size'] >= 10.5:
        if norm in vocab:
            return norm.title()
        if norm.startswith('coverage policy'):
            return 'Coverage Policy'
    if line['size'] >= 9.5 and line['x0'] < 70:
        if norm in vocab:
            return norm.title()
    if line['size'] >= 12.0 and line['x0'] < 70:
        if norm in vocab:
            return norm.title()
    if line['size'] >= 7.5 and text.replace(' ', '').isupper():
        stripped = norm.replace(' ', '')
        for sv in vocab:
            if sv.replace(' ', '') == stripped:
                return sv.title()
    return None


def classify_subsection(line: dict) -> Optional[str]:
    """Return heading text if line is a subsection heading, else None."""
    # Table interior lines are never subsection headings
    if line.get('in_table'):
        return None
    _non_bold_exact_only = False
    if not line['bold']:
        if line['x0'] >= 70 or line['size'] < 9.5:
            return None
        _non_bold_exact_only = True
        # Fall through to vocab check for non-bold left-margin lines
    elif line['size'] < 7.0 or line['size'] >= 11.5:
        return None
    elif line['x0'] > 120:
        return None
    if line['size'] < 7.0 or line['size'] >= 11.5:
        return None
    text = line['text']
    tl = text.lower()
    if any(f in tl for f in BOILERPLATE_FRAGMENTS):
        return None
    # Never classify continuation lines (start lowercase) as headings
    if text and text[0].islower():
        return None
    if text.startswith('•') or text.startswith('\u2022'):
        return None
    # Never classify numbered items as subsections
    if re.match(r'^\d+\.', text):
        return None

    norm          = normalize_heading(text)
    norm_nospace  = norm.replace(' ', '')
    norm_collapsed = text.strip().replace(' ', '').lower()

    def _proper_case(sv: str) -> str:
        ACRONYMS = {'fda', 'iv', 'sc', 'dvt', 'pe', 'vte', 'hae', 'gi',
                    'nccn', 'aca', 'acc', 'aha', 'nla', 'tg', 'pa', 'nms'}
        def _cap_word(w: str) -> str:
            parts = w.split('-')
            return '-'.join(
                p.upper() if p.lower() in ACRONYMS else p.capitalize()
                for p in parts)
        slash_parts = sv.split('/')
        return '/'.join(' '.join(_cap_word(w) for w in sp.split())
                        for sp in slash_parts)

    # Non-bold lines: exact match only, no startswith
    if _non_bold_exact_only:
        for sv in SUBSECTION_VOCAB:
            sv_nospace = sv.replace(' ', '').replace('/', '')
            if norm_collapsed == sv_nospace:
                return _proper_case(sv)
        return None

    for sv in sorted(SUBSECTION_VOCAB, key=len, reverse=True):
        sv_nospace = sv.replace(' ', '').replace('/', '')
        # Exact match after normalization
        if norm_collapsed == sv_nospace:
            return _proper_case(sv)
        # Letter-spaced match: 'P OLICY S TATEMENT' → norm_nospace='policystatement'
        if norm_nospace == sv_nospace:
            _raw = text.strip()
            _is_letter_spaced = (
                _raw.replace(' ', '').upper() == _raw.replace(' ', '') and
                len(_raw.split()) > len(sv.split())
            )
            if _is_letter_spaced:
                return _proper_case(sv)
            return _raw if len(_raw) > len(sv) else _proper_case(sv)
        # Near-exact: garbled match (handles 1-3 missing/dropped chars)
        if (len(norm_collapsed) >= len(sv_nospace) - 3 and
                len(norm_collapsed) <= len(sv_nospace) and
                _is_garbled_match(norm_collapsed, sv_nospace)):
            return _proper_case(sv)
    return None


def classify_subsubsection(line: dict) -> str | None:
    """Return heading text if line is a sub-subsection heading, else None.
    
    Sub-subsection headings are italic (not bold), left-margin, short,
    start with a capital letter, and are not boilerplate.
    """
    if line.get('bold'):
        return None
    if not line.get('italic'):
        return None
    if line.get('in_table'):
        return None
    if line['size'] < 9.0:
        return None
    if line['x0'] >= 70:
        return None
    text = line['text'].strip()
    if not text or not text[0].isupper():
        return None
    if text.startswith('•') or text.startswith('\u2022'):
        return None
    # Must be short — body text lines are long prose
    if len(text) > 80:
        return None
    # Exclude boilerplate starts
    _boilerplate_starts = (
        'the following', 'policies are', 'certain cigna',
        'evidence of', 'coverage policies', 'reimbursement',
        'when billing', 'authorization', 'please note',
        'service agreement', 'companies and',
    )
    tl = text.lower()
    if any(tl.startswith(b) for b in _boilerplate_starts):
        return None
    return text


def classify_mm_subsection(line: dict,
                            current_section: str = '') -> Optional[tuple]:
    if not line.get('bold') or line.get('in_table'):
        return None
    if line['size'] < 9.5 or line['size'] >= 14.0:
        return None
    if line['x0'] > 120:
        return None

    text = line['text'].strip()
    if not text or not text[0].isupper():
        return None

    tl = text.lower()
    if any(s in tl for s in ('instructions for use',
                              'table of contents',
                              'related coverage resources')):
        return None

    sec = current_section.lower()

    # Level 1: underline flag pre-stamped by cigna_parse.py
    if line.get('underline'):
        return (text, 1)

    # Level 2 only in specific sections
    if sec not in ('general background', 'coverage policy',
                   'coding information', 'health equity considerations',
                   'medicare coverage determinations'):
        return None

    if len(text.split()) > 12:
        return None
    if text.endswith('.'):
        return None

    return (text, 2)
