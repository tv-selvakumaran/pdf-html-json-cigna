#!/usr/bin/env python3
"""
cigna_parse_headings.py
=======================
Section and subsection heading classifiers for the Cigna V5 converter.
"""

from __future__ import annotations
import re
from typing import Optional
from cigna_constants import SECTION_VOCAB, SUBSECTION_VOCAB, BOILERPLATE_FRAGMENTS
from cigna_extractor import _has_underline_rect


def normalize_heading(text: str) -> str:
    """Remove letter-spacing artifacts: 'P OLICY S TATEMENT' → 'policystatement'."""
    stripped = text.strip()
    no_spaces = stripped.replace(' ', '')
    if no_spaces.isupper() and len(no_spaces) >= 4:
        return no_spaces.lower()
    t = text
    for _ in range(5):
        t2 = re.sub(r'(?<=[A-Z]) (?=[A-Z]{2,})', '', t)
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
                     vocab: set = None, is_cpg: bool = False) -> Optional[str]:
    if vocab is None:
        from cigna_constants import SECTION_VOCAB
        vocab = SECTION_VOCAB
    if not line['bold']:
        return None

    # Primary signal: line sits inside a colored section heading rect
    # Exemptions: IFU and PURPOSE are detected separately
    _has_rect = line.get('in_section_rect', False)

    text = line['text']
    norm = normalize_heading(text)

    _cpg_subsection_lookalikes = {
        'generalbackground', 'documentationguidelines', 'literaturereview',
    }
    if is_cpg and norm in _cpg_subsection_lookalikes:
        return None

    if _has_rect:
        # Colored rect present — trust vocab match at any reasonable size
        if line['size'] >= 9.0 and line['x0'] < 200:
            if norm in vocab:
                return norm.title()
            if norm.startswith('coverage policy'):
                return 'Coverage Policy'
            if norm == 'reference':
                return 'References'
    else:
        # No colored rect — only match if large font (genuine section heading)
        # This covers IFU ("INSTRUCTIONS FOR USE"), Overview in drug docs, etc.
        # Excludes phrases that can also appear as plain bold TABLE TITLES
        # (e.g. "FDA Approved Indication", "FDA Recommended Dosing", "Drug
        # Availability") -- those must go through the rect-present path
        # above to count as a genuine section boundary; without a colored
        # rect, they're just a table's own title, not a real heading.
        _table_title_lookalikes = {
            'fda approved indication', 'fda recommended dosing',
            'drug availability',
        }
        if line['size'] >= 12.0 and line['x0'] < 70 and norm not in _table_title_lookalikes:
            if norm in vocab:
                return norm.title()
            if norm == 'reference':
                return 'References'
        # Letter-spaced all-caps (e.g. "A PPENDIX") -- require the text to
        # actually HAVE internal spacing to reconstruct (multiple tokens),
        # not just happen to be a single all-caps word with no spaces at
        # all (e.g. a small emphasized sub-heading like "OVERVIEW" inside
        # a Background section, which should NOT restart a top-level
        # section boundary).
        if (line['size'] >= 7.5 and ' ' in text.strip() and
                text.replace(' ', '').isupper() and
                norm not in _table_title_lookalikes):
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

    # Pass 1: exact or letter-spaced match takes priority over any
    # garbled match, regardless of vocabulary iteration order -- this
    # prevents a short, valid phrase (e.g. "Medically Necessary") from
    # being swallowed by a longer entry that's a near-superset of it
    # (e.g. "Not Medically Necessary") under the garbled-match
    # tolerance below.
    for sv in sorted(SUBSECTION_VOCAB, key=len, reverse=True):
        sv_nospace = sv.replace(' ', '').replace('/', '')
        if norm_collapsed == sv_nospace:
            return _proper_case(sv)
        if norm_nospace == sv_nospace:
            _raw = text.strip()
            _is_letter_spaced = (
                _raw.replace(' ', '').upper() == _raw.replace(' ', '') and
                len(_raw.split()) > len(sv.split())
            )
            if _is_letter_spaced:
                return _proper_case(sv)
            return _raw if len(_raw) > len(sv) else _proper_case(sv)

    # Pass 2: no exact match anywhere -- fall back to garbled matching
    # (handles 1-3 missing/dropped characters from OCR-like extraction
    # artifacts).
    for sv in sorted(SUBSECTION_VOCAB, key=len, reverse=True):
        sv_nospace = sv.replace(' ', '').replace('/', '')
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


def classify_drug_subsection(line: dict,
                              current_section: str = '') -> Optional[tuple]:
    if not line.get('bold') or line.get('in_table'):
        return None
    if line['size'] < 9.5 or line['size'] >= 14.0:
        return None
    if line['x0'] > 120:
        return None

    text = line['text'].strip()
    if not text:
        return None

    sec = current_section.lower()
    from cigna_constants import DRUG_SUBSECTION_VOCAB
    norm_lc = normalize_heading(text)
    sec_vocab = DRUG_SUBSECTION_VOCAB.get(sec, set())
    if norm_lc not in sec_vocab:
        return None
    return (text, 2)


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
    from cigna_constants import MM_SUBSECTION_VOCAB
    _sec_entry = MM_SUBSECTION_VOCAB.get(sec, set())
    if isinstance(_sec_entry, dict):
        _generic_vocab = _sec_entry.get('generic', set())
        _doc_specific_vocab = _sec_entry.get('document-specific', set())
    else:
        _generic_vocab = _sec_entry
        _doc_specific_vocab = set()

    norm_lc = normalize_heading(text)

    # Level 1: underline flag pre-stamped by cigna_parse.py, matched
    # against the GENERIC vocabulary.
    if sec in ('general background', 'background'):
        if norm_lc in _generic_vocab:
            return (text, 1)
    else:
        if line.get('underline'):
            if norm_lc in _generic_vocab:
                return (text, 1)

    # Level 2 only in specific sections, matched against the
    # DOCUMENT-SPECIFIC vocabulary, gated by looser structural criteria
    # (no underline required, but font size/word-count/punctuation
    # constraints instead).
    if sec not in ('general background', 'background', 'coverage policy',
                   'coding information', 'health equity considerations',
                   'medicare coverage determinations', 'administrative policy'):
        return None
    if len(text.split()) > 8:
        return None
    if text.endswith('.'):
        return None
    if text.endswith(':'):
        return None
    if norm_lc not in _doc_specific_vocab:
        return None

    return (text, 2)


def classify_mm_general_background_subsubsection(
        phrases: list, phrase_idx: int,
        current_section: str = '', current_subsection: str = '') -> tuple | None:
    """
    ... (docstring as before, now also gated on current_section)
    """
    from cigna_constants import MM_SUBSUBSECTION_VOCAB
    _sec = current_section.lower()
    _subsec = current_subsection.lower()
    _allowed = MM_SUBSUBSECTION_VOCAB.get(_sec, {}).get(_subsec, set())
    if not _allowed:
        return None

    if phrase_idx >= len(phrases) or not phrases[phrase_idx]['bold']:
        return None

    _lead_in_parts = []
    i = phrase_idx
    while i < len(phrases) and phrases[i]['bold']:
        _lead_in_parts.append(phrases[i]['text'])
        i += 1
    _lead_in = ' '.join(_lead_in_parts)

    if ':' not in _lead_in and i < len(phrases):
        _next_text = phrases[i]['text']
        _colon_pos = _next_text.find(':')
        if _colon_pos != -1:
            _lead_in = _lead_in + _next_text[:_colon_pos + 1]
        else:
            return None

    if not _lead_in.rstrip().endswith(':'):
        return None

    _heading_text = _lead_in.rstrip(':').strip()
    from cigna_parse_headings import normalize_heading
    norm = normalize_heading(_heading_text)

    _parts = [normalize_heading(p.strip()) for p in _heading_text.split('/')]
    if len(_parts) > 1:
        if all(p in _allowed for p in _parts):
            return (_heading_text, i - 1)
    elif norm in _allowed:
        return (_heading_text, i - 1)

    return None



def classify_cpg_subsection(line: dict, current_section: str = '') -> tuple | None:
    """
    Detect cpg-family General Background subsection headings:
    Description, General Background, Documentation Guidelines,
    Literature Review -- all-caps, bold, underlined, standalone line.
    """
    if not line.get('bold') or not line.get('underline') or line.get('in_table'):
        return None
    if current_section.lower() != 'general background':
        return None

    text = line['text'].strip()
    if not text or not text.replace(' ', '').isupper():
        return None

    from cigna_parse_headings import normalize_heading
    norm = normalize_heading(text)

    _cpg_gb_subsections = {
        'description', 'generalbackground', 'documentationguidelines', 'literaturereview',
    }
    if norm in _cpg_gb_subsections:
        return (text.title(), 1)

    return None
