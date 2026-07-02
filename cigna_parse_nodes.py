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


# ════════════════════════════════════════════════════════════════════════════
# Node dataclasses
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Node:
    kind: str = 'node'

@dataclass
class ParagraphBlockNode(Node):
    block: ParagraphBlock = field(default_factory=ParagraphBlock)
    kind: str = 'paragraph_block'

@dataclass
class TableNode(Node):
    html: str = ''
    table_type: str = 'generic'
    page: int = 0
    top: float = 0.0
    title: str = ''
    kind: str = 'table'

@dataclass
class FootnoteNode(Node):
    text: str = ''
    kind: str = 'footnote'

@dataclass
class IFUNode(Node):
    text: str = ''
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
    kind: str = 'subsubsection'

@dataclass
class SectionNode(Node):
    heading: str = ''
    children: list = field(default_factory=list)
    kind: str = 'section'
    page: int = 0      # page number where this section heading appears
    top:  float = 0.0  # y-coordinate of heading (for bbox comparison)

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
# Constants
# ════════════════════════════════════════════════════════════════════════════

SECTION_VOCAB = {
    'overview', 'coverage policy', 'references', 'revision details',
    'coding information', 'general information', 'background',
    'appendix', 'definitions', 'instructions for use',
    'medical necessity criteria', 'reauthorization criteria',
    'authorization duration', 'conditions not covered',
    'product criteria', 'other uses with supportive evidence',
    'disease overview', 'guidelines', 'safety', 'recommendations',
}

MM_SECTION_VOCAB = {
    'overview', 'coverage policy', 'references', 'revision details',
    'coding information', 'general information', 'background',
    'appendix', 'definitions', 'instructions for use',
    'medical necessity criteria', 'reauthorization criteria',
    'authorization duration',
    'general background',
    'health equity considerations',
    'medicare coverage determinations',
    'scope', 'procedure', 'standard procedure',
    'attachments', 'compliance measure',
    'state/federal guidelines', 'state/federal compliance',
}

SUBSECTION_VOCAB = {
    'policy statement', 'drug quantity limits', 'general information',
    'notes', 'medically necessary', 'not medically necessary',
    'experimental', 'investigational',
    'considered medically necessary', 'considered not medically necessary',
    'dosing', 'availability', 'dose escalation', 'criteria', 'indications',
    'fda-approved indications', 'fda-approved indication',
    'compendium indications', 'off-label use', 'place in therapy',
    'background', 'contraindications', 'warnings', 'guidelines',
    'other uses with supportive evidence', 'guidelines/scientific statements',
    'clinical evidence', 'monitoring', 'administration',
    'coverage criteria', 'authorization criteria', 'quantity limits',
    'reauthorization criteria', 'authorization duration',
    'medical necessity criteria',
}

BOILERPLATE_FRAGMENTS = [
    'instructions for use',
    'confidential, unpublished property of cigna',
    'do not duplicate or distribute',
    'use and distribution limited solely',
    '© copyright cigna',
    'cigna coverage policies',
    'individual coverage determinations',
    'this coverage policy is subject to',
    'reserved for cigna',
    'cigna does not endorse',
]

FOOTER_PATTERNS = [
    re.compile(r'^Page\s+\d+\s+of\s+\d+', re.I),
    re.compile(r'^Coverage\s+Policy\s+Number\s*:', re.I),
]


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

def is_footer(top: float, text: str) -> bool:
    if top > 715:
        return True
    return any(p.match(text) for p in FOOTER_PATTERNS)
