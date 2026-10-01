#!/usr/bin/env python3
"""Shared constants for the Cigna PDF pipeline."""



# ════════════════════════════════════════════════════════════════════════════
# Exceptions for Coding Tables (CPT/HCPCS/ICD) appearing in Coverage Policy sections
# ════════════════════════════════════════════════════════════════════════════

_CODING_TABLE_SECTION_EXCEPTIONS = ( 'mm_0554', )



# ════════════════════════════════════════════════════════════════════════════
# Vocabulary Constants
# ════════════════════════════════════════════════════════════════════════════

SECTION_VOCAB = {
    'overview', 'coverage policy', 'references', 'revision details',
    'coding information', 'general information', 'background',
    'appendix', 'appendix a', 'appendix b', 
    'appendix 1', 'appendix 2', 'appendix 3',
    'definitions', 'instructions for use', 'coding',
    'medical necessity criteria', 'reauthorization criteria',
    'authorization duration', 'conditions not covered',
    'product criteria', 'other uses with supportive evidence',
    'disease overview', 'guidelines', 'safety', 'recommendations',
}


PH_SECTION_VOCAB = SECTION_VOCAB | {
    'general background',
    'recommended dosing',
    'fda approved indication',
    'fda approved indications',
    'fda recommended dosing',
}


MM_SECTION_VOCAB = {
    'overview', 'coverage policy', 'references', 'revision details',
    'coding information', 'general information', 'background',
    'appendix', 'definitions', 'instructions for use',
    'medical necessity criteria', 'reauthorization criteria',
    'authorization duration', 'administrative policy',
    'general background',
    'health equity considerations',
    'medicare coverage determinations',
    'scope', 'procedure', 'standard procedure',
    'attachments', 'compliance measure',
    'state/federal guidelines', 'state/federal compliance',
}


CPG_SECTION_VOCAB = MM_SECTION_VOCAB | {
    'guidelines',
    'literature review',
    'description',
    'documentation guidelines',
}


SUBSECTION_VOCAB = {
    'policy statement', 'drug quantity limits', 'general information',
    'notes', 'medically necessary', 'not medically necessary',
    'experimental', 'investigational', 'dosing information', 
    'considered medically necessary', 'considered not medically necessary',
    'dosing', 'availability', 'dose escalation', 'criteria', 'indications',
    'fda-approved indications', 'fda-approved indication',
    'compendium indications', 'off-label use', 'place in therapy',
    'background', 'contraindications', 'warnings', 'guidelines',
    'other uses with supportive evidence', 'guidelines/scientific statements',
    'clinical evidence', 'monitoring', 'administration',
    'coverage criteria', 'authorization criteria', 'quantity limits',
    'reauthorization criteria', 'authorization duration',
    'medical necessity criteria', 'disease overview',
    'conditions not covered', 'overview', 'safety',
}


MM_SUBSECTION_VOCAB = {
    'coding information': {
        'wellness examinations',
        'preventive care screenings and interventions',
        'code group 1', 'code group 2', 'code group 3',
        'code group 4', 'code group 5', 'code group 6',
        'code group 7', 'code group 8', 'code group 9',
        'code group 10', 'code group 11', 'code group 12',
        'code group 13', 'code group 14', 'code group 15',
    },
    'administrative policy': {
        'additional preventive care services',
        'reasonable medical management',
        'reporting preventive care services',
        'modifier 33',
    },
    'general background': {
        'generic': {
            'u.s. food and drug administration (fda)',
            'literature review',
            'professional societies/organizations',
        },
        'document-specific': {
            'diagnostic nasal/sinus endoscopy',
            'functional endoscopic sinus surgery (fess)',
            'turbinectomy',
            }
    },
}


DRUG_SUBSECTION_VOCAB = {
    'coding information': {
        'blepharospasm',
        'cervical dystonia',
        'hyperhidrosis, primary axillary',
        'migraine headache prevention',
        'neurogenic detrusor overactivity (ndo), pediatric',
        'overactive bladder with symptoms of urge urinary incontinence, urgency, and frequency (adult)',
        'spasticity, limb(s)',
        'spasticity, upper limb(s)',
        'strabismus',
        'urinary incontinence due to detrusor overactivity associated with a neurological condition (adult)',
        'achalasia',
        'anal fissure, chronic',
        'dystonia, focal upper limb',
        'essential tremor',
        'hemifacial spasm',
        'hyperhidrosis, gustatory',
        'hyperhidrosis, primary palmar/plantar/facial',
        'laryngeal dystonia (spasmodic dysphonia)',
        'oromandibular dystonia',
        'sialorrhea, chronic',
    },
}


MM_SUBSUBSECTION_VOCAB = {
    'general background': {
        'professional societies/organizations': {
            'american academy of allergy, asthma and immunology (aaaai)',
            'american college of allergy, asthma and immunology (acaai)',
            'american academy of otolaryngology-head and neck surgery (aao-hns)',
            'american rhinologic society (ars)',
        },
    },
    'coverage policy': {
        'medically necessary': {
            'general criteria for medically necessary lab testing',
        },
    },
}


# ════════════════════════════════════════════════════════════════════════════
# Boilerplate Constants
# ════════════════════════════════════════════════════════════════════════════

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



import re

# ════════════════════════════════════════════════════════════════════════════
# Footer Patterns
# ════════════════════════════════════════════════════════════════════════════

FOOTER_PATTERNS = [
    re.compile(r'^Page\s+\d+\s+of\s+\d+', re.I),
    re.compile(r'^Coverage\s+Policy\s+Number\s*:', re.I),
    re.compile(r'^(Drug\s+Coverage\s+Policy|Administrative\s+Policy|'
               r'Medical\s+Coverage\s+Policy|Coverage\s+Policy)\s*:', re.I),
    # CPG-family running footer: "<Title> (CPG ###)" -- title varies,
    # so match on the "(CPG ###)" suffix at end of line.
    re.compile(r'^.*\(CPG\s*\d+\)\s*$', re.I),
    # Drug/Pharmacy-family running footers -- distinct wording from the
    # existing "Drug Coverage Policy:" pattern above (note "and Biologic").
    re.compile(r'^Drug\s+and\s+Biologic\s+Coverage\s+Policy\s*:\s*\S+', re.I),
    re.compile(r'^Pharmacy\s+Benefit\s+Clinical\s+Criteria\s*:\s*\S+', re.I),
]




# ════════════════════════════════════════════════════════════════════════════
# Citation Organization Names
# ════════════════════════════════════════════════════════════════════════════

# Purpose-built list of organization names exactly as they appear in
# in-text citations (title case, no parenthetical abbreviation) --
# kept separate from MM_SUBSUBSECTION_VOCAB (which stores lowercase,
# full names with abbreviations, for subsection-heading matching) to
# avoid unreliable case/format transformation between the two uses.
CITATION_ORG_NAMES = {
    ('Agency for Healthcare Research and Quality', 'AHQR'),
    ('American College of Obstetricians and Gynecologists', 'ACOG'),
    ('American Rhinologic Society', 'ARS'),
    ('American Society for Clinical Oncology', 'ASCO'),
    ('American Society of Reproductive Medicine', 'ASRM'),
    ('American Society for Reproductive Medicine', 'ASRM'),
    ('American Urological Association', 'AUA'),
    ('Centers for Disease Control and Prevention', 'CDC'),
    ('Institute for Clinical Systems Improvement', 'ICSI'),
    ('National Comprehensive Cancer Network', 'NCCN'),
    ('Royal College of Obstetricians and Gynaecologists', 'RCOG'),
}



# ════════════════════════════════════════════════════════════════════════════
# Citation Patterns
# ════════════════════════════════════════════════════════════════════════════

_org_pattern = '|'.join(
    re.escape(variant)
    for full_name, abbr in sorted(CITATION_ORG_NAMES, key=lambda t: len(t[0]), reverse=True)
    for variant in (full_name, abbr)
)


_CITATION_PATTERN = re.compile(
    r'\b('
    r'(?:(?:[A-Z]\.?\s+)?[A-Z][A-Za-z\-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z\-]+)?'
    r'(?:,?\s+et\s+al\.?)?'
    rf'|{_org_pattern}'
    r')'
    r',?\s+(\d{4})'
    r')\b'
)


_NON_SURNAME_WORDS = {
    'in', 'the', 'a', 'an', 'on', 'at', 'by', 'for', 'since', 'during',
    'january', 'february', 'march', 'april', 'may', 'june', 'july',
    'august', 'september', 'october', 'november', 'december',
}
