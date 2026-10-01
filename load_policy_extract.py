#!/usr/bin/env python3
"""
Phase 1 loader for policy_extract: parses PDFs via the existing
cigna_parse pipeline, walks the resulting CignaDoc tree for TableNodes
of type hcpcs/cpt/icd/revision and for the ReferencesNode, and upserts
their row data into the policy_extract database (documents,
policy_codes, revision_history, policy_references).

Idempotent: re-running for a document deletes and reinserts its rows
in a single transaction, so repeated runs (e.g. after a parsing fix)
never duplicate data.

Usage:
    python3 load_policy_extract.py <pdf_or_dir> [<pdf_or_dir> ...] \\
        --corpus drug --dsn "postgresql://user:pass@host/policy_extract"

    # Dry run (no DB writes, just print what would be inserted):
    python3 load_policy_extract.py <pdf_path> --corpus drug --dry-run
"""
import argparse
import sys
from pathlib import Path


def _walk_table_nodes(node, out):
    cls_name = type(node).__name__
    if cls_name == 'TableNode':
        out.append(node)
    children = getattr(node, 'children', None)
    if children:
        for c in children:
            _walk_table_nodes(c, out)


def _find_references_node(doc):
    """doc.nodes is a flat top-level list; ReferencesNode replaces the
    old SectionNode('References') at the top level -- no recursion
    needed."""
    for n in doc.nodes:
        if type(n).__name__ == 'ReferencesNode':
            return n
    return None


def extract_document_payload(doc, pdf_path: Path, corpus: str) -> dict:
    """
    Parse one PDF and return a dict ready for loading:
      {
        'policy_id': str,
        'policy_number': str | None,
        'title': str,
        'source_url': str,
        'corpus': str,
        'policy_codes': [ {code, code_type, description, display_order}, ... ],
        'revision_history': [ {revision_type, summary, revision_date, display_order}, ... ],
        'policy_references': [ {item_num, ref_type, text}, ... ],
      }
    """
    table_nodes = []
    for n in doc.nodes:
        _walk_table_nodes(n, table_nodes)

    policy_codes = []
    revision_history = []
    prod_criteria_tables = []  # [ {title, footnote, plan_category, start_page, end_page,
                                #    col_headings, display_order, rows: [{product_name, criteria_text, display_order}]} ]
    pref_criteria_tables = []  # [ {title, footnote, start_page, end_page,
                                #    col_headings, display_order, rows: [{subgroup_heading, product_name, criteria_text, display_order}]} ]
    non_covered_criteria_tables = []
    _ncc_table_order = 0

    mcd_tables = []
    _mcd_table_order = 0

    moa_tables = []
    _moa_table_order = 0

    fda_device_mfg_tables = []
    _fda_table_order = 0

    appendix_med_tables = []
    _appendix_table_order = 0

    two_col_heading_tables = []
    _tch_table_order = 0

    _pc_table_order = 0
    _pc_table_order2 = 0
    for tn in table_nodes:
        rows = getattr(tn, 'rows', None) or []
        data_rows = [r for r in rows if r.row_kind != 'header']

        if tn.table_type in ('hcpcs', 'cpt', 'icd'):
            for i, r in enumerate(data_rows):
                cells = r.cells
                if not cells or not cells[0] or not str(cells[0]).strip():
                    continue
                policy_codes.append({
                    'code': str(cells[0]).strip(),
                    'code_type': tn.table_type,
                    'description': (str(cells[1]).strip()
                                     if len(cells) > 1 and cells[1] else None),
                    'display_order': i,
                })

        elif tn.table_type == 'two_col_heading':
            from cigna_parse_tables import consolidate_two_col_heading_rows
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            tch_rows = [
                {'section_heading': s, 'col1_value': c1, 'col2_value': c2, 'display_order': i}
                for i, (s, c1, c2) in enumerate(consolidate_two_col_heading_rows(rows_with_labels))
            ]

            if tch_rows:
                title = getattr(tn, 'title', '') or ''
                two_col_heading_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _tch_table_order,
                    'rows': tch_rows,
                })
                _tch_table_order += 1

        elif tn.table_type == 'revision':
            for i, r in enumerate(data_rows):
                cells = r.cells
                if not cells or len(cells) < 2:
                    continue
                summary = cells[1] if len(cells) > 1 else None
                if not summary or not str(summary).strip():
                    continue
                revision_history.append({
                    'revision_type': (str(cells[0]).strip()
                                       if cells[0] else None),
                    'summary': str(summary).strip(),
                    'revision_date': (str(cells[2]).strip()
                                        if len(cells) > 2 and cells[2] else None),
                    'display_order': i,
                })

        elif tn.table_type == 'prod_criteria':
            from cigna_parse_tables import _normalize_plan_category, consolidate_prod_criteria_rows
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            pc_rows = [
                {'product_name': p, 'criteria_text': c, 'display_order': i}
                for i, (p, c) in enumerate(consolidate_prod_criteria_rows(rows_with_labels))
            ]

            if pc_rows:
                title = getattr(tn, 'title', '') or ''
                prod_criteria_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'plan_category': _normalize_plan_category(title) or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _pc_table_order,
                    'rows': pc_rows,
                })
                _pc_table_order += 1

        elif tn.table_type == 'pref_criteria':
            from cigna_parse_tables import consolidate_pref_criteria_rows
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            pref_rows = []
            _current_subgroup = None
            _row_order = 0
            for product_name, criteria_text, is_subheader in consolidate_pref_criteria_rows(rows_with_labels):
                if is_subheader:
                    _current_subgroup = product_name  # the subheader's text IS the heading
                    continue
                if not criteria_text:
                    continue
                pref_rows.append({
                    'subgroup_heading': _current_subgroup,
                    'product_name': product_name,
                    'criteria_text': criteria_text,
                    'display_order': _row_order,
                })
                _row_order += 1

            if pref_rows:
                title = getattr(tn, 'title', '') or ''
                pref_criteria_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _pc_table_order2,
                    'rows': pref_rows,
                })
                _pc_table_order2 += 1

        elif tn.table_type == 'criteria':
            from cigna_parse_tables import _normalize_plan_category, consolidate_prod_criteria_rows
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            ncc_rows = [
                {'product_name': p, 'criteria_text': c, 'display_order': i}
                for i, (p, c) in enumerate(consolidate_prod_criteria_rows(rows_with_labels))
            ]

            if ncc_rows:
                title = getattr(tn, 'title', '') or ''
                non_covered_criteria_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'plan_category': _normalize_plan_category(title) or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _ncc_table_order,
                    'rows': ncc_rows,
                })
                _ncc_table_order += 1

        elif tn.table_type == 'medicare_coverage_determination':
            from cigna_parse_tables import extract_medicare_coverage_determinations
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            mcd_rows = [
                {**d, 'display_order': i}
                for i, d in enumerate(extract_medicare_coverage_determinations(rows_with_labels))
            ]

            if mcd_rows:
                title = getattr(tn, 'title', '') or ''
                mcd_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _mcd_table_order,
                    'rows': mcd_rows,
                })
                _mcd_table_order += 1

        elif tn.table_type == 'moa':
            from cigna_parse_tables import consolidate_moa_rows
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            moa_rows = []
            _current_subgroup = None
            _row_order = 0
            _pending = []

            def _flush_pending():
                nonlocal _row_order
                for drug_name, moa, indications in consolidate_moa_rows(_pending):
                    if not drug_name and not indications:
                        continue
                    moa_rows.append({
                        'subgroup_heading': _current_subgroup,
                        'drug_name': drug_name,
                        'mechanism_of_action': moa,
                        'indications': indications,
                        'display_order': _row_order,
                    })
                    _row_order += 1
                _pending.clear()

            for lbl, row in rows_with_labels:
                if lbl == 'header':
                    continue
                if lbl == 'subheader':
                    _flush_pending()
                    _text = next((c for c in (row or []) if c and str(c).strip()), '')
                    _current_subgroup = str(_text).strip()
                    continue
                _pending.append((lbl, row))
            _flush_pending()

            if moa_rows:
                title = getattr(tn, 'title', '') or ''
                moa_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _moa_table_order,
                    'rows': moa_rows,
                })
                _moa_table_order += 1

        elif tn.table_type == 'fda_device_mfg':
            from cigna_parse_tables import extract_fda_device_mfg_rows
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            rows_with_labels = [(r.row_kind, r.cells) for r in rows]
            fda_rows = [
                {**d, 'display_order': i}
                for i, d in enumerate(extract_fda_device_mfg_rows(rows_with_labels))
            ]

            if fda_rows:
                title = getattr(tn, 'title', '') or ''
                fda_device_mfg_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _fda_table_order,
                    'rows': fda_rows,
                })
                _fda_table_order += 1

        elif tn.table_type == 'appendix_med':
            header_row = next((r for r in rows if r.row_kind == 'header'), None)
            col_headings = []
            if header_row is not None:
                col_headings = [str(c).strip() for c in header_row.cells if c and str(c).strip()]

            appendix_rows = []
            _row_order = 0
            for r in data_rows:
                if r.row_kind == 'header':
                    continue
                cells = r.cells
                _nonempty = [str(c).strip() for c in cells if c and str(c).strip()]
                if not _nonempty:
                    continue
                medication = _nonempty[0] if len(_nonempty) > 0 else ''
                mode = _nonempty[1] if len(_nonempty) > 1 else ''
                if not medication:
                    continue
                appendix_rows.append({
                    'medication': medication,
                    'mode_of_administration': mode or None,
                    'display_order': _row_order,
                })
                _row_order += 1

            if appendix_rows:
                title = getattr(tn, 'title', '') or ''
                appendix_med_tables.append({
                    'title': title,
                    'footnote': getattr(tn, 'footnote', '') or None,
                    'start_page': getattr(tn, 'page', None),
                    'end_page': getattr(tn, 'end_page', None) or getattr(tn, 'page', None),
                    'col_headings': col_headings or None,
                    'display_order': _appendix_table_order,
                    'rows': appendix_rows,
                })
                _appendix_table_order += 1

    meta = getattr(doc, 'meta', None) or {}
    policy_id = pdf_path.stem
    policy_number = meta.get('policy_id') if isinstance(meta, dict) else None
    title = meta.get('title', '') if isinstance(meta, dict) else ''
    source_url = meta.get('source_url', '') if isinstance(meta, dict) else ''

    policy_references = []
    refs_node = _find_references_node(doc)
    if refs_node is not None:
        for it in getattr(refs_node, 'items', []) or []:
            text = (getattr(it, 'text', '') or '').strip()
            if not text:
                continue
            policy_references.append({
                'item_num': getattr(it, 'item_num', None),
                'ref_type': getattr(it, 'ref_type', None) or 'citation',
                'text': text,
            })

    return {
        'policy_id': policy_id,
        'policy_number': policy_number,
        'title': title,
        'source_url': source_url,
        'corpus': corpus,
        'policy_codes': policy_codes,
        'revision_history': revision_history,
        'policy_references': policy_references,
        'prod_criteria_tables': prod_criteria_tables,
        'pref_criteria_tables': pref_criteria_tables,
        'non_covered_criteria_tables': non_covered_criteria_tables,
        'mcd_tables': mcd_tables,
        'moa_tables': moa_tables,
        'fda_device_mfg_tables': fda_device_mfg_tables,
        'appendix_med_tables': appendix_med_tables,
        'two_col_heading_tables': two_col_heading_tables,
    }



def extract_document_paragraphs(doc, pdf_path: Path, corpus: str) -> dict:
    """
    Given an already-parsed CignaDoc tree, return a dict of
    General Background/Background narrative content, ready for
    loading:
      {
        'sections': [...], 'subsections': [...], 'paragraphs': [...],
        'paragraph_items': [...], 'paragraph_societies': [...],
        'paragraph_citation_links': [...],
      }
    """
    sections_payload = []
    subsections_payload = []
    paragraphs_payload = []
    paragraph_items_payload = []
    paragraph_societies_payload = []
    paragraph_citation_links_payload = []

    _sec_order = 0
    for node in doc.nodes:
        _tname = type(node).__name__
        if _tname not in ('SectionNode', 'ReferencesNode'):
            continue

        _sec_key = {'heading': node.heading, 'display_order': _sec_order}
        sections_payload.append(_sec_key)
        _sec_order += 1

        if _tname == 'ReferencesNode':
            continue  # content lives in policy_references, not paragraphs

        _this_section_ref = _sec_key  # resolved to a real id at load time
        _subsec_order = 0
        _para_order = 0

        def _extract_items(items, paragraph_ref, parent_item_ref=None, order_box=None):
            if order_box is None:
                order_box = [0]
            for it in items:
                _tname = type(it).__name__
                _kind_map = {
                    'BulletItem': 'bullet', 'NumItem': 'num', 'NoteItem': 'note',
                    'LetterItem': 'letter', 'RomanItem': 'roman',
                    'ParenNumItem': 'paren_num', 'PlainText': 'plain',
                }
                _kind = _kind_map.get(_tname, 'plain')
                _text = getattr(it, 'text', '') or ''
                _item_ref = {
                    'paragraph_ref': paragraph_ref,
                    'parent_item_ref': parent_item_ref,
                    'kind': _kind,
                    'text': _text,
                    'item_number': getattr(it, 'number', None),
                    'display_order': order_box[0],
                }
                order_box[0] += 1
                paragraph_items_payload.append(_item_ref)

                for ni in getattr(it, 'notes', []) or []:
                    _note_ref = {
                        'paragraph_ref': paragraph_ref,
                        'parent_item_ref': _item_ref,
                        'kind': 'note',
                        'text': getattr(ni, 'text', '') or '',
                        'item_number': None,
                        'display_order': order_box[0],
                    }
                    order_box[0] += 1
                    paragraph_items_payload.append(_note_ref)

                _children = getattr(it, 'children', None)
                if _children:
                    _extract_items(_children, paragraph_ref, _item_ref, order_box)

        def _emit_paragraph(block, page, top, subsection_ref):
            nonlocal _para_order
            _para_ref = {
                'section_ref': _this_section_ref,
                'subsection_ref': subsection_ref,
                'zone': getattr(block, 'zone', None),
                'page': page,
                'top': top,
                'display_order': _para_order,
            }
            _para_order += 1
            paragraphs_payload.append(_para_ref)

            if block.plain_text:
                paragraph_items_payload.append({
                    'paragraph_ref': _para_ref, 'parent_item_ref': None,
                    'kind': 'plain', 'text': block.plain_text,
                    'item_number': None, 'display_order': 0,
                })
            elif block.items:
                _extract_items(block.items, _para_ref)

            for _society in (getattr(block, 'society_names', None) or []):
                paragraph_societies_payload.append({
                    'paragraph_ref': _para_ref, 'society_name': _society,
                    'display_order': len(paragraph_societies_payload),
                })

            for _link in (getattr(block, 'citation_links', None) or []):
                paragraph_citation_links_payload.append({
                    'paragraph_ref': _para_ref,
                    'mention_text': _link['mention'],
                    'item_num': _link['item_num'],
                    'matched': _link['item_num'] is not None,
                    'display_order': len(paragraph_citation_links_payload),
                })

        def _walk(children, subsection_ref):
            nonlocal _subsec_order
            for child in children:
                _tname = type(child).__name__
                if _tname in ('SubsectionNode', 'SubSubsectionNode'):
                    _sub_ref = {
                        'section_ref': _this_section_ref,
                        'heading': child.heading,
                        'level': 1 if _tname == 'SubsectionNode' else 2,
                        'display_order': _subsec_order,
                    }
                    _subsec_order += 1
                    subsections_payload.append(_sub_ref)
                    _walk(child.children, _sub_ref)
                elif _tname == 'ParagraphBlockNode':
                    block = getattr(child, 'block', None)
                    if block is None:
                        continue
                    _emit_paragraph(block, child.page, child.top, subsection_ref)

        _walk(node.children, None)

    return {
        'sections': sections_payload,
        'subsections': subsections_payload,
        'paragraphs': paragraphs_payload,
        'paragraph_items': paragraph_items_payload,
        'paragraph_societies': paragraph_societies_payload,
        'paragraph_citation_links': paragraph_citation_links_payload,
    }


def upsert_document(conn, payload: dict) -> str:
    """Upsert the documents row, returning its id. Idempotent by
    policy_id (unique)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (policy_id, policy_number, title, source_url, corpus, updated_at)
            VALUES (%(policy_id)s, %(policy_number)s, %(title)s, %(source_url)s, %(corpus)s, now())
            ON CONFLICT (policy_id) DO UPDATE SET
                policy_number = EXCLUDED.policy_number,
                title = EXCLUDED.title,
                source_url = EXCLUDED.source_url,
                corpus = EXCLUDED.corpus,
                updated_at = now()
            RETURNING id
            """,
            payload,
        )
        return cur.fetchone()[0]


def replace_policy_codes(conn, document_id: str, rows: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM policy_codes WHERE document_id = %s", (document_id,))
        if rows:
            cur.executemany(
                """
                INSERT INTO policy_codes (document_id, code, code_type, description, display_order)
                VALUES (%(document_id)s, %(code)s, %(code_type)s, %(description)s, %(display_order)s)
                """,
                [{**r, 'document_id': document_id} for r in rows],
            )


def replace_revision_history(conn, document_id: str, rows: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM revision_history WHERE document_id = %s", (document_id,))
        if rows:
            cur.executemany(
                """
                INSERT INTO revision_history
                    (document_id, revision_type, summary, revision_date, display_order)
                VALUES
                    (%(document_id)s, %(revision_type)s, %(summary)s, %(revision_date)s, %(display_order)s)
                """,
                [{**r, 'document_id': document_id} for r in rows],
            )


def replace_policy_references(conn, document_id: str, rows: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM policy_references WHERE document_id = %s", (document_id,))
        if rows:
            cur.executemany(
                """
                INSERT INTO policy_references (document_id, item_num, ref_type, text)
                VALUES (%(document_id)s, %(item_num)s, %(ref_type)s, %(text)s)
                """,
                [{**r, 'document_id': document_id} for r in rows],
            )


def replace_prod_criteria(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        # DELETE on prod_criteria_metadata cascades to product_criteria
        # via FK ON DELETE CASCADE.
        cur.execute("DELETE FROM prod_criteria_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO prod_criteria_metadata
                    (document_id, title, footnote, plan_category, start_page, end_page,
                     col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s, %(plan_category)s,
                     %(start_page)s, %(end_page)s, %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO product_criteria
                        (document_id, prod_criteria_metadata_id, product_name,
                         criteria_text, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s, %(product_name)s,
                         %(criteria_text)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_pref_criteria(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        # DELETE on pref_criteria_metadata cascades to preferred_criteria
        # via FK ON DELETE CASCADE.
        cur.execute("DELETE FROM pref_criteria_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO pref_criteria_metadata
                    (document_id, title, footnote, start_page, end_page,
                     col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s,
                     %(start_page)s, %(end_page)s, %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO preferred_criteria
                        (document_id, pref_criteria_metadata_id, subgroup_heading,
                         product_name, criteria_text, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s, %(subgroup_heading)s,
                         %(product_name)s, %(criteria_text)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_non_covered_criteria(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM non_covered_criteria_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO non_covered_criteria_metadata
                    (document_id, title, plan_category, footnote, start_page, end_page,
                     col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(plan_category)s, %(footnote)s,
                     %(start_page)s, %(end_page)s, %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO non_covered_criteria
                        (document_id, non_covered_criteria_metadata_id, product_name,
                         criteria_text, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s, %(product_name)s,
                         %(criteria_text)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_medicare_coverage_determinations(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM medicare_coverage_determination_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO medicare_coverage_determination_metadata
                    (document_id, title, footnote, start_page, end_page, col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s, %(start_page)s, %(end_page)s,
                     %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO medicare_coverage_determinations
                        (document_id, medicare_coverage_determination_metadata_id,
                         determination_type, contractor, determination_name,
                         revision_effective_date, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s, %(determination_type)s,
                         %(contractor)s, %(determination_name)s,
                         %(revision_effective_date)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_mechanism_of_action(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM moa_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO moa_metadata
                    (document_id, title, footnote, start_page, end_page, col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s, %(start_page)s, %(end_page)s,
                     %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO mechanism_of_action
                        (document_id, moa_metadata_id, subgroup_heading,
                         drug_name, mechanism_of_action, indications, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s, %(subgroup_heading)s,
                         %(drug_name)s, %(mechanism_of_action)s, %(indications)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_fda_device_mfg(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fda_device_mfg_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO fda_device_mfg_metadata
                    (document_id, title, footnote, start_page, end_page, col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s, %(start_page)s, %(end_page)s,
                     %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO fda_devices
                        (document_id, fda_device_mfg_metadata_id,
                         device_or_product, identifier, manufacturer, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s,
                         %(device_or_product)s, %(identifier)s, %(manufacturer)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_appendix_medications(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM appendix_med_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO appendix_med_metadata
                    (document_id, title, footnote, start_page, end_page, col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s, %(start_page)s, %(end_page)s,
                     %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO appendix_medications
                        (document_id, appendix_med_metadata_id,
                         medication, mode_of_administration, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s,
                         %(medication)s, %(mode_of_administration)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )


def replace_two_col_heading(conn, document_id: str, tables: list):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM two_col_heading_metadata WHERE document_id = %s", (document_id,))
        for t in tables:
            cur.execute(
                """
                INSERT INTO two_col_heading_metadata
                    (document_id, title, footnote, start_page, end_page, col_headings, display_order)
                VALUES
                    (%(document_id)s, %(title)s, %(footnote)s, %(start_page)s, %(end_page)s,
                     %(col_headings)s, %(display_order)s)
                RETURNING id
                """,
                {**t, 'document_id': document_id},
            )
            metadata_id = cur.fetchone()[0]
            rows = t.get('rows') or []
            if rows:
                cur.executemany(
                    """
                    INSERT INTO two_col_heading_rows
                        (document_id, two_col_heading_metadata_id,
                         section_heading, col1_value, col2_value, display_order)
                    VALUES
                        (%(document_id)s, %(metadata_id)s,
                         %(section_heading)s, %(col1_value)s, %(col2_value)s, %(display_order)s)
                    """,
                    [{**r, 'document_id': document_id, 'metadata_id': metadata_id} for r in rows],
                )
 

def load_one_tables(conn, doc, pdf_path: Path, corpus: str, dry_run: bool = False) -> str | None:
    payload = extract_document_payload(doc, pdf_path, corpus)

    _pc_row_count = sum(len(t['rows']) for t in payload['prod_criteria_tables'])
    _pref_row_count = sum(len(t['rows']) for t in payload['pref_criteria_tables'])
    _ncc_row_count = sum(len(t['rows']) for t in payload['non_covered_criteria_tables'])
    _mcd_row_count = sum(len(t['rows']) for t in payload['mcd_tables'])
    _moa_row_count = sum(len(t['rows']) for t in payload['moa_tables'])
    _fda_row_count = sum(len(t['rows']) for t in payload['fda_device_mfg_tables'])
    _appendix_row_count = sum(len(t['rows']) for t in payload['appendix_med_tables'])
    _tch_row_count = sum(len(t['rows']) for t in payload['two_col_heading_tables'])
    
    print(f"{pdf_path.name}: policy_codes={len(payload['policy_codes'])} "
          f"revision_history={len(payload['revision_history'])} "
          f"policy_references={len(payload['policy_references'])} "
          f"prod_criteria_tables={len(payload['prod_criteria_tables'])} "
          f"(rows={_pc_row_count})"
          f"pref_criteria_tables={len(payload['pref_criteria_tables'])} "
          f"(rows={_pref_row_count})"
          f"non_covered_criteria_tables={len(payload['non_covered_criteria_tables'])} "
          f"(rows={_ncc_row_count})"
          f"mcd_tables={len(payload['mcd_tables'])} "
          f"(rows={_mcd_row_count})"
          f"moa_tables={len(payload['moa_tables'])} "
          f"(rows={_moa_row_count})"
          f"fda_tables={len(payload['fda_device_mfg_tables'])} "
          f"(rows={_fda_row_count})"
          f"appendix_med_tables={len(payload['appendix_med_tables'])} "
          f"(rows={_appendix_row_count})"
          f"two_col_heading_tables={len(payload['two_col_heading_tables'])} "
          f"(rows={_tch_row_count})")

    if dry_run:
        for c in payload['policy_codes'][:3]:
            print(f"    [code sample] {c}")
        for r in payload['revision_history'][:3]:
            print(f"    [revision sample] {r}")
        for ref in payload['policy_references'][:3]:
            print(f"    [reference sample] {ref}")
        for t in payload['prod_criteria_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [prod_criteria table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['pref_criteria_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [pref_criteria table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['non_covered_criteria_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [non_covered_criteria table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['mcd_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [mcd table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['moa_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [moa table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['fda_device_mfg_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [fda table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['appendix_med_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [appendix_med table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        for t in payload['two_col_heading_tables'][:2]:
            _t_display = {k: v for k, v in t.items() if k != 'rows'}
            print(f"    [two_col_heading table sample] {_t_display}")
            for r in t['rows'][:2]:
                print(f"        [row sample] {r}")
        return None

    document_id = upsert_document(conn, payload)
    replace_policy_codes(conn, document_id, payload['policy_codes'])
    replace_revision_history(conn, document_id, payload['revision_history'])
    replace_policy_references(conn, document_id, payload['policy_references'])
    replace_prod_criteria(conn, document_id, payload['prod_criteria_tables'])
    replace_pref_criteria(conn, document_id, payload['pref_criteria_tables'])
    replace_non_covered_criteria(conn, document_id, payload['non_covered_criteria_tables'])
    replace_medicare_coverage_determinations(conn, document_id, payload['mcd_tables'])
    replace_mechanism_of_action(conn, document_id, payload['moa_tables'])
    replace_fda_device_mfg(conn, document_id, payload['fda_device_mfg_tables'])
    replace_appendix_medications(conn, document_id, payload['appendix_med_tables'])
    replace_two_col_heading(conn, document_id, payload['two_col_heading_tables'])
    conn.commit()
    return document_id


def load_one_paragraphs(conn, doc, document_id: str | None, pdf_path: Path, corpus: str, dry_run: bool = False):
    paragraphs_payload = extract_document_paragraphs(doc, pdf_path, corpus)

    print(f"{pdf_path.name}: sections={len(paragraphs_payload['sections'])} "
          f"subsections={len(paragraphs_payload['subsections'])} "
          f"paragraphs={len(paragraphs_payload['paragraphs'])} "
          f"paragraph_items={len(paragraphs_payload['paragraph_items'])} "
          f"citation_links={len(paragraphs_payload['paragraph_citation_links'])}")

    if dry_run:
        for s in paragraphs_payload['sections'][:2]:
            print(f"    [section sample] {s}")
        for sub in paragraphs_payload['subsections'][:3]:
            print(f"    [subsection sample] {sub}")
        for p in paragraphs_payload['paragraphs'][:3]:
            print(f"    [paragraph sample] zone={p.get('zone')} page={p.get('page')}")
        for it in paragraphs_payload['paragraph_items'][:3]:
            print(f"    [paragraph_item sample] kind={it['kind']} text={it['text'][:60]!r}")
        for soc in paragraphs_payload['paragraph_societies'][:3]:
            print(f"    [paragraph_society sample] {soc['society_name']}")
        for link in paragraphs_payload['paragraph_citation_links'][:5]:
            print(f"    [citation_link sample] mention={link['mention_text']!r} "
                  f"item_num={link['item_num']} matched={link['matched']}")
        return

    replace_general_background_content(conn, document_id, paragraphs_payload)
    conn.commit()



def replace_general_background_content(conn, document_id: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sections WHERE document_id = %s", (document_id,))

        section_ids = {}  # id(section_ref_dict) -> real db id
        for s in payload['sections']:
            cur.execute(
                """
                INSERT INTO sections (document_id, heading, display_order)
                VALUES (%(document_id)s, %(heading)s, %(display_order)s)
                RETURNING id
                """,
                {**s, 'document_id': document_id},
            )
            section_ids[id(s)] = cur.fetchone()[0]

        subsection_ids = {}
        for sub in payload['subsections']:
            _sec_id = section_ids[id(sub['section_ref'])]
            cur.execute(
                """
                INSERT INTO subsections (document_id, section_id, heading, level, display_order)
                VALUES (%(document_id)s, %(section_id)s, %(heading)s, %(level)s, %(display_order)s)
                RETURNING id
                """,
                {'document_id': document_id, 'section_id': _sec_id,
                 'heading': sub['heading'], 'level': sub['level'],
                 'display_order': sub['display_order']},
            )
            subsection_ids[id(sub)] = cur.fetchone()[0]

        paragraph_ids = {}
        for p in payload['paragraphs']:
            _sec_id = section_ids[id(p['section_ref'])]
            _sub_id = subsection_ids.get(id(p['subsection_ref'])) if p.get('subsection_ref') else None
            cur.execute(
                """
                INSERT INTO paragraphs (document_id, section_id, subsection_id, zone, page, top, display_order)
                VALUES (%(document_id)s, %(section_id)s, %(subsection_id)s, %(zone)s, %(page)s, %(top)s, %(display_order)s)
                RETURNING id
                """,
                {'document_id': document_id, 'section_id': _sec_id, 'subsection_id': _sub_id,
                 'zone': p.get('zone'), 'page': p.get('page'), 'top': p.get('top'),
                 'display_order': p['display_order']},
            )
            paragraph_ids[id(p)] = cur.fetchone()[0]

        item_ids = {}
        # Insert items in payload order; a parent item is always
        # emitted before its children (see extract_document_paragraphs'
        # recursive _extract_items), so parent_item_ref is always
        # already resolved by the time a child item is processed.
        for it in payload['paragraph_items']:
            _para_id = paragraph_ids[id(it['paragraph_ref'])]
            _parent_id = item_ids.get(id(it['parent_item_ref'])) if it.get('parent_item_ref') else None
            cur.execute(
                """
                INSERT INTO paragraph_items
                    (document_id, paragraph_id, parent_item_id, kind, text, item_number, display_order)
                VALUES
                    (%(document_id)s, %(paragraph_id)s, %(parent_item_id)s, %(kind)s, %(text)s,
                     %(item_number)s, %(display_order)s)
                RETURNING id
                """,
                {'document_id': document_id, 'paragraph_id': _para_id, 'parent_item_id': _parent_id,
                 'kind': it['kind'], 'text': it['text'], 'item_number': it.get('item_number'),
                 'display_order': it['display_order']},
            )
            item_ids[id(it)] = cur.fetchone()[0]

        for soc in payload['paragraph_societies']:
            _para_id = paragraph_ids[id(soc['paragraph_ref'])]
            cur.execute(
                """
                INSERT INTO paragraph_societies (document_id, paragraph_id, society_name, display_order)
                VALUES (%(document_id)s, %(paragraph_id)s, %(society_name)s, %(display_order)s)
                """,
                {'document_id': document_id, 'paragraph_id': _para_id,
                 'society_name': soc['society_name'], 'display_order': soc['display_order']},
            )

        for link in payload['paragraph_citation_links']:
            _para_id = paragraph_ids[id(link['paragraph_ref'])]
            cur.execute(
                """
                INSERT INTO paragraph_citation_links
                    (document_id, paragraph_id, mention_text, item_num, matched, display_order)
                VALUES
                    (%(document_id)s, %(paragraph_id)s, %(mention_text)s, %(item_num)s,
                     %(matched)s, %(display_order)s)
                """,
                {'document_id': document_id, 'paragraph_id': _para_id,
                 'mention_text': link['mention_text'], 'item_num': link['item_num'],
                 'matched': link['matched'], 'display_order': link['display_order']},
            )



def collect_pdfs(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            yield from sorted(p.glob('*.pdf'))
        elif p.suffix.lower() == '.pdf':
            yield p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+', help='PDF file(s) or directory(ies)')
    ap.add_argument('--corpus', required=True, choices=['drug', 'medical-administrative'])
    ap.add_argument('--dsn', default=None, help='postgresql://... connection string')
    ap.add_argument('--dry-run', action='store_true',
                     help='Parse and print counts only, no DB writes')
    args = ap.parse_args()

    pdfs = list(collect_pdfs(args.paths))
    print(f"Found {len(pdfs)} PDF(s) to process")

    conn = None
    if not args.dry_run:
        if not args.dsn:
            print("ERROR: --dsn is required unless --dry-run is set", file=sys.stderr)
            sys.exit(1)
        import psycopg2
        conn = psycopg2.connect(args.dsn)

    ok, err = 0, 0
    for pdf in pdfs:
        try:
            from cigna_parse import parse
            doc, _ = parse(pdf)
            document_id = load_one_tables(conn, doc, pdf, args.corpus, dry_run=args.dry_run)
            load_one_paragraphs(conn, doc, document_id, pdf, args.corpus, dry_run=args.dry_run)
            ok += 1
        except Exception as ex:
            err += 1
            print(f"  FAILED: {pdf.name}: {ex}", file=sys.stderr)
            if conn:
                conn.rollback()

    print(f"\nDone. ok={ok} err={err}")
    if conn:
        conn.close()


if __name__ == '__main__':
    main()
