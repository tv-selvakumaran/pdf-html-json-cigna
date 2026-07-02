"""
Reconstruct Cigna drug availability table from pdfplumber extraction.
Returns clean HTML table string.
"""

def reverse_cell(text):
    """Reverse a cell string, handling newlines and None."""
    if not text or not text.strip():
        return ''
    return text.replace('\n', '').strip()[::-1].strip()


def is_reversed_drug_name(text):
    """Check if cell looks like a reversed drug name (letters/hyphens only)."""
    if not text or not text.strip():
        return False
    t = text.replace('\n', '').strip()
    if len(t) < 3:
        return False
    # Reversed drug names contain only letters, hyphens, spaces
    import re
    return bool(re.match(r'^[A-Za-z\-\s]+$', t))


def reconstruct_availability_table(table_data):
    """
    Reconstruct Cigna drug availability table from pdfplumber output.
    
    Returns HTML string for the table.
    
    table_data: list of rows, each row a list of cell strings
    """
    if not table_data or len(table_data) < 3:
        return ''

    # ── Step 1: Find drug names and their tick columns ────────────────────
    # Scan rows 0-1 for reversed drug name strings
    # Group by x-position (column index) to merge multi-column names
    
    drug_groups = {}  # tick_col → drug_name
    
    # First pass: collect all reversed name cells from rows 0-1
    name_cells = {}  # col_idx → reversed_text
    for row_idx in range(min(2, len(table_data))):
        for col_idx, cell in enumerate(table_data[row_idx]):
            if cell and is_reversed_drug_name(cell):
                rev = reverse_cell(cell)
                if col_idx in name_cells:
                    name_cells[col_idx] = name_cells[col_idx] + rev
                else:
                    name_cells[col_idx] = rev

    # Merge consecutive name columns where first ends with '-'
    # e.g. col13='ustekinumab-' + col14='aauz' → 'ustekinumab-aauz'
    sorted_name_cols = sorted(name_cells.keys())
    merged_names = {}
    skip_next = False
    for i, nc in enumerate(sorted_name_cols):
        if skip_next:
            skip_next = False
            continue
        name = name_cells[nc]
        # Check if next col is adjacent and current ends with '-'
        if (name.endswith('-') and
                i + 1 < len(sorted_name_cols) and
                sorted_name_cols[i+1] == nc + 1):
            merged_names[nc] = name + name_cells[sorted_name_cols[i+1]]
            skip_next = True
        else:
            merged_names[nc] = name
    name_cells = merged_names

    # Second pass: find tick columns from data rows (rows 2+)
    tick_cols = set()
    for row_idx in range(2, len(table_data)):
        for col_idx, cell in enumerate(table_data[row_idx]):
            if cell and cell.strip() == '√':
                tick_cols.add(col_idx)

    # Tick col is always exactly name_col - 1
    drug_col_map = {}  # tick_col → drug_name
    for nc, name in name_cells.items():
        tick_col = nc - 1
        if tick_col in tick_cols:
            drug_col_map[tick_col] = name

    if not drug_col_map:
        return ''

    # ── Step 2: Extract row headers and dosages ───────────────────────────
    # col=0: row type (Autoinjector, PFS, Vial) — reversed
    # col=1: dosage strength — normal text
    
    type_map = {'tcejniotuA': 'Autoinjector', 'SFP': 'PFS',
                'laiV': 'Vial', 'Autoinject': 'Autoinjector'}
    rows_data = []
    prev_type = ''
    for row_idx in range(2, len(table_data)):
        row = table_data[row_idx]
        raw_type = (row[0] or '').strip()
        rev = reverse_cell(raw_type)
        row_type = type_map.get(raw_type, type_map.get(rev, rev))
        if row_type:
            prev_type = row_type
        else:
            row_type = prev_type  # inherit from previous
        dosage = ''
        if len(row) > 1 and row[1]:
            dosage = row[1].replace('\n', ' ').strip()
        if not dosage:
            continue  # skip rows with no dosage
        # Collect tick marks
        ticks = {}
        for col_idx, cell in enumerate(row):
            if cell and cell.strip() == '\u221a':  # √
                if col_idx in drug_col_map:
                    ticks[drug_col_map[col_idx]] = '\u2713'  # ✓
        rows_data.append({
            'type':   row_type,
            'dosage': dosage,
            'ticks':  ticks,
        })

    if not rows_data:
        return ''

    # ── Step 3: Render HTML table ─────────────────────────────────────────
    from html import escape as esc
    
    # Get ordered drug list (by tick_col order)
    drugs = [drug_col_map[tc] for tc in sorted(drug_col_map.keys())]
    # Deduplicate preserving order
    seen = set()
    unique_drugs = []
    for d in drugs:
        if d not in seen:
            seen.add(d)
            unique_drugs.append(d)
    drugs = unique_drugs

    lines = []
    lines.append('  <table border="1" bordercolor="#000000" cellpadding="4" '
                 'cellspacing="0" style="width:100%;font-size:9pt">')
    lines.append('   <thead>')
    lines.append('    <tr style="background:#e8e8e8">')
    lines.append('     <th>Dosage Form</th>')
    lines.append('     <th>Strength</th>')
    for drug in drugs:
        lines.append(f'     <th style="writing-mode:vertical-rl;'
                     f'transform:rotate(180deg);max-width:30px">'
                     f'{esc(drug)}</th>')
    lines.append('    </tr>')
    lines.append('   </thead>')
    lines.append('   <tbody>')

    for rd in rows_data:
        row_type = rd['type']
        dosage = rd['dosage']
        lines.append('    <tr>')
        lines.append(f'     <td>{esc(row_type)}</td>')
        lines.append(f'     <td>{esc(dosage)}</td>')
        for drug in drugs:
            mark = rd['ticks'].get(drug, '')
            lines.append(f'     <td style="text-align:center">{esc(mark)}</td>')
        lines.append('    </tr>')

    lines.append('   </tbody>')
    lines.append('  </table>')
    return '\n'.join(lines)


if __name__ == '__main__':
    # Quick test
    import pdfplumber, os
    from pathlib import Path

    path = Path(os.path.expanduser(
        "~/Data/AIAgents/MedicalAIAgent/payer-policy-data/pdf-data/cigna/drug/"
        "all-policy-documents/dqm_001_coveragepositioncriteria_ustekinumab_subcutaneous.pdf"
    ))

    with pdfplumber.open(str(path)) as pdf:
        page = pdf.pages[2]
        tables = page.find_tables()
        if tables:
            table_data = tables[0].extract()
            html = reconstruct_availability_table(table_data)
            print(html[:2000] if html else "No table reconstructed")


def reconstruct_drug_quantity_table(page4_data, page5_data, gray_bottom=None):
    """
    Reconstruct Cigna Drug Quantity Limits table.

    Handles two formats:
    1. Standard format: 3-col header in rows 0-1, data rows follow
    2. Per-days format: multi-row header (rows 0-N where col0 empty),
       single or few data rows

    page4_data: table extraction from page containing Drug Quantity Limits
    page5_data: continuation page (or None)
    Returns HTML string.
    """
    from html import escape as esc

    if not page4_data:
        return ''

    def clean_cell(cell):
        if not cell:
            return ''
        return cell.replace('\n', ' ').strip()

    def fmt_cell(txt):
        if not txt:
            return ''
        parts = [p.strip() for p in txt.strip().split('\n') if p.strip()]
        if not parts:
            return ''
        result = esc(parts[0])
        for p in parts[1:]:
            if p.startswith('•') or p.startswith('\u2022') or p.startswith('-'):
                result += '<br>' + esc(p)
            else:
                result += ' ' + esc(p)
        return result

    # ── Detect format: find first data row ──────────────────────────────
    # Use gray_bottom (gray background = header rows) if available.
    # gray_bottom is the y-coord of the bottom of the last gray rect.
    # All pdfplumber rows above gray_bottom are header rows.
    # We count header rows by scanning pdfplumber rows top-to-bottom
    # and stopping when we pass gray_bottom (proxy: when col0 has
    # a drug name, not a column-header keyword).
    _hdr_words = {'product', 'strength', 'and', 'form', 'retail', 'home',
                  'delivery', 'maximum', 'quantity', 'limit', 'limits',
                  'per', 'days', 'none', ''}
    data_start = 1
    if gray_bottom is not None:
        # Find first row where col0 is NOT a header keyword
        for ri, row in enumerate(page4_data):
            c0 = (row[0] or '').strip().lower()
            c0_words = set(c0.split())
            if c0_words and not c0_words.issubset(_hdr_words):
                data_start = ri
                break
    else:
        for ri, row in enumerate(page4_data[1:], 1):
            if (row[0] or '').strip():
                data_start = ri
                break

    # ── If multi-row header (data_start > 1): collapse header rows ────────
    if data_start > 1:
        ncols = max(len(row) for row in page4_data)
        # Collapse header rows into single row by joining non-empty cells per col
        header = []
        for ci in range(ncols):
            parts = []
            for ri in range(data_start):
                cell = (page4_data[ri][ci] if ci < len(page4_data[ri]) else '') or ''
                if cell.strip():
                    parts.append(cell.strip())
            header.append(' '.join(parts) if parts else None)

        # Collect all data rows from both pages
        all_data = list(page4_data[data_start:])
        if page5_data:
            for row in page5_data:
                if any((c or '').strip() for c in (row or [])):
                    all_data.append(row)

        if not all_data:
            return ''

        # Determine which columns have content
        _header_cols = {ci for ci in range(ncols) if (header[ci] or '').strip()}
        _data_cols   = {ci for ci in range(ncols)
                        if any(((row[ci] if ci < len(row) else '') or '').strip()
                               for row in all_data)}
        used_cols = sorted(_header_cols | _data_cols) or list(range(ncols))

        # Remap header labels to match data column positions.
        # pdfplumber places merged-cell header text in the leftmost cell of
        # the span, but data lands one column to the left of the label.
        # e.g. header['Retail Qty'] at col3, data at col2 → remap to col2.
        remapped = list(header)
        for ci in sorted(_header_cols - _data_cols):
            # Header col with no data — try shifting label one col left
            target = ci - 1
            if (target in _data_cols and
                    target in set(used_cols) and
                    not (remapped[target] or '').strip()):
                remapped[target] = header[ci]
                remapped[ci] = None
        # Rebuild used_cols excluding cols that lost their header via remap
        # and have no data either
        used_cols = sorted(
            ci for ci in used_cols
            if (remapped[ci] or '').strip() 
        )

        lines = ['  <table border="1" bordercolor="#000000" cellpadding="4" '
                 'cellspacing="0" style="width:100%;font-size:9pt">',
                 '   <thead>',
                 '    <tr style="background:#e8e8e8">']
        for ci in used_cols:
            lines.append(f'     <th>{fmt_cell(remapped[ci] or "")}</th>')
        lines += ['    </tr>', '   </thead>', '   <tbody>']

        # Pre-compute rowspans for the first column (Product)
        _col0_idx = used_cols[0]
        _rowspans = []
        i = 0
        while i < len(all_data):
            row = all_data[i]
            c0 = (row[_col0_idx] if _col0_idx < len(row) else '') or ''
            if c0.strip():
                span = 1
                j = i + 1
                while j < len(all_data):
                    next_c0 = ((all_data[j][_col0_idx]
                               if _col0_idx < len(all_data[j]) else '') or '')
                    if not next_c0.strip():
                        span += 1
                        j += 1
                    else:
                        break
                _rowspans.append(span)
                _rowspans.extend([0] * (span - 1))
                i += span
            else:
                _rowspans.append(0)
                i += 1

        for ri, row in enumerate(all_data):
            cells = [(row[ci] if ci < len(row) else '') or '' for ci in used_cols]
            if not any(c.strip() for c in cells):
                continue
            lines.append('    <tr>')
            for ci_idx, c in enumerate(cells):
                if ci_idx == 0:
                    rs = _rowspans[ri] if ri < len(_rowspans) else 1
                    if rs == 0:
                        continue
                    elif rs > 1:
                        lines.append(
                            f'     <td style="vertical-align:top" '
                            f'rowspan="{rs}">{fmt_cell(c)}</td>')
                    else:
                        lines.append(
                            f'     <td style="vertical-align:top">{fmt_cell(c)}</td>')
                else:
                    lines.append(
                        f'     <td style="vertical-align:top">{fmt_cell(c)}</td>')
            lines.append('    </tr>')

        lines += ['   </tbody>', '  </table>']
        return '\n'.join(lines)

    # ── Standard format: original reconstruction logic ────────────────────
    h0 = clean_cell(page4_data[0][0] if len(page4_data[0]) > 0 else '')
    h1 = clean_cell(page4_data[0][1] if len(page4_data[0]) > 1 else '')
    h2a = clean_cell(page4_data[0][3] if len(page4_data[0]) > 3 else '')
    h2b = clean_cell(page4_data[1][3] if len(page4_data) > 1 and
                     len(page4_data[1]) > 3 else '')
    h2 = f"{h2a} {h2b}".strip()

    row2 = page4_data[2] if len(page4_data) > 2 else []
    row3 = page4_data[3] if len(page4_data) > 3 else []
    row4 = page4_data[4] if len(page4_data) > 4 else []

    products_p4 = (row2[0] or '').strip() if row2 else ''
    products_p5 = ''
    if page5_data and len(page5_data) > 0:
        products_p5 = (page5_data[0][0] or '').strip() if page5_data[0] else ''
    full_products = products_p4
    if products_p5:
        full_products = full_products + '\n' + products_p5
    product_lines = [p.strip() for p in full_products.split('\n') if p.strip()]
    products_html = '<br>'.join(esc(p) for p in product_lines)

    str1 = clean_cell(row2[1] if len(row2) > 1 else '')
    str2 = clean_cell(row3[1] if len(row3) > 1 else '')
    str3 = clean_cell(row4[1] if len(row4) > 1 else '')
    qty1 = clean_cell(row2[2] if len(row2) > 2 else '')
    qty2 = qty1
    qty3 = clean_cell(row4[2] if len(row4) > 2 else '')

    strengths_html = '<br>'.join(esc(s) for s in [str1, str2, str3] if s)
    qtys_html = '<br>'.join(esc(q) for q in [qty1, qty2, qty3] if q)

    non_covered_rows = []
    if page5_data and len(page5_data) > 1:
        for row in page5_data[1:]:
            if not row:
                continue
            c0 = clean_cell(row[0] if len(row) > 0 else '')
            c1 = (row[1] or '').strip() if len(row) > 1 else ''
            c2 = clean_cell(row[2] if len(row) > 2 else '')
            if c0 or c1 or c2:
                non_covered_rows.append((c0, c1, c2))

    lines = []
    lines.append('  <table border="1" bordercolor="#000000" cellpadding="4" '
                 'cellspacing="0" style="width:100%;font-size:9pt">')
    lines.append('   <thead>')
    lines.append('    <tr style="background:#e8e8e8">')
    for h in [h0, h1, h2]:
        lines.append(f'     <th>{esc(h)}</th>')
    lines.append('    </tr>')
    lines.append('   </thead>')
    lines.append('   <tbody>')
    lines.append('    <tr>')
    lines.append(f'     <td style="vertical-align:top">{products_html}</td>')
    lines.append(f'     <td style="vertical-align:top">{strengths_html}</td>')
    lines.append(f'     <td style="vertical-align:top">{qtys_html}</td>')
    lines.append('    </tr>')
    for c0, c1, c2 in non_covered_rows:
        lines.append('    <tr>')
        lines.append(f'     <td>{esc(c0)}</td>')
        c1_parts = [l.strip() for l in c1.split('\n') if l.strip()]
        c1_html = '<br>'.join(esc(l) for l in c1_parts) if c1_parts else ''
        lines.append(f'     <td>{c1_html}</td>')
        lines.append(f'     <td>{esc(c2)}</td>')
        lines.append('    </tr>')
    lines.append('   </tbody>')
    lines.append('  </table>')
    return '\n'.join(lines)


def reconstruct_revision_table(page1_data, page2_data=None):
    """
    Reconstruct Cigna Revision Details table from pdfplumber extractions.

    page1_data: table extraction from page with 'Type of Revision' header
    page2_data: continuation page table (or None)

    Returns HTML string.
    """
    from html import escape as esc

    if not page1_data:
        return ''

    def clean_cell(cell):
        if not cell:
            return ''
        return cell.replace('\n', ' ').strip()

    # Header row: Type of Revision | Summary of Changes | Date
    h0 = clean_cell(page1_data[0][0] if page1_data[0] else '')
    h1 = clean_cell(page1_data[0][1] if len(page1_data[0]) > 1 else '')
    h2 = clean_cell(page1_data[0][2] if len(page1_data[0]) > 2 else '')

    # Collect all data rows from both pages
    all_rows = []
    for row in page1_data[1:]:
        if not row:
            continue
        c0 = clean_cell(row[0] if len(row) > 0 else '')
        c1 = clean_cell(row[1] if len(row) > 1 else '')
        c2 = clean_cell(row[2] if len(row) > 2 else '')
        if c0 or c1 or c2:
            all_rows.append((c0, c1, c2))

    if page2_data:
        for ri, row in enumerate(page2_data):
            if not row:
                continue
            c0 = clean_cell(row[0] if len(row) > 0 else '')
            c1 = clean_cell(row[1] if len(row) > 1 else '')
            c2 = clean_cell(row[2] if len(row) > 2 else '')
            if not (c0 or c1 or c2):
                continue
            # First row of continuation with empty col0 is a wrap-around
            # from last row of previous page — append to last row's col1
            if ri == 0 and not c0 and not c2 and c1 and all_rows:
                last = list(all_rows[-1])
                last[1] = (last[1] + ' ' + c1).strip()
                all_rows[-1] = tuple(last)
            else:
                all_rows.append((c0, c1, c2))

    if not all_rows:
        return ''

    # Carry forward revision type for rows with empty col0
    prev_type = ''
    lines = []
    lines.append('  <table border="1" bordercolor="#000000" cellpadding="4" '
                 'cellspacing="0" style="width:100%;font-size:9pt">')
    lines.append('   <thead>')
    lines.append('    <tr style="background:#e8e8e8">')
    for h in [h0, h1, h2]:
        lines.append(f'     <th>{esc(h)}</th>')
    lines.append('    </tr>')
    lines.append('   </thead>')
    lines.append('   <tbody>')

    for c0, c1, c2 in all_rows:
        if c0:
            prev_type = c0
        else:
            c0 = prev_type
        lines.append('    <tr>')
        lines.append(f'     <td style="vertical-align:top">{esc(c0)}</td>')
        lines.append(f'     <td style="vertical-align:top">{esc(c1)}</td>')
        lines.append(f'     <td style="white-space:nowrap">{esc(c2)}</td>')
        lines.append('    </tr>')

    lines.append('   </tbody>')
    lines.append('  </table>')
    return '\n'.join(lines)


def reconstruct_moa_table(tdata: list, gray_bottom: float = None) -> str:
    """
    Reconstruct Mechanism of Action appendix table.
    gray_bottom: y-coordinate of bottom of last gray header rect (from page.rects).
                 Used to reliably detect where header rows end and data begins.
    """
    from html import escape as esc

    if not tdata:
        return ''

    def clean(cell):
        if not cell:
            return ''
        return cell.replace('\n', ' ').strip()

    # Detect if this is a header page or continuation
    flat_r02 = ' '.join(str(c) for row in tdata[:3] for c in (row or []))
    is_header_page = 'Mechanism of Action' in flat_r02

    data_start = 0
    headers = ['Biologics', 'Mechanism of Action', 'Examples of Indications*']

    if is_header_page:
        # Extract actual header text from rows 0-2
        h_biologics = ''
        h_mechanism = ''
        h_indications = ''
        for row in tdata[:3]:
            for c in (row or []):
                cs = (c or '').strip()
                if 'Biologics' in cs:
                    h_biologics = cs
                elif 'Mechanism of Action' in cs:
                    h_mechanism = cs
                elif 'Indications' in cs or 'Examples' in cs:
                    h_indications = (h_indications + ' ' + cs).strip()
        if h_biologics or h_mechanism:
            headers = [h_biologics or 'Biologics',
                       h_mechanism or 'Mechanism of Action',
                       h_indications or 'Examples of Indications*']
        # Use gray_bottom to find data_start if available
        # gray_bottom is the y of the last header rect bottom
        # We count rows until their content is clearly a drug name
        header_words = {'biologics', 'mechanism', 'action', 'examples',
                        'indications', 'inflammatory', 'none', ''}
        for ri, row in enumerate(tdata):
            c0 = (row[0] if row else '') or ''
            c0s = c0.strip()
            if c0s and c0s.lower() not in header_words and ri >= 1:
                data_start = ri
                break
        else:
            data_start = 3
        # Back up data_start to include any subgroup heading rows that
        # immediately precede the first data row (e.g. 'Biologics' on row 1)
        while data_start > 1:
            prev_row = tdata[data_start - 1] if data_start - 1 < len(tdata) else []
            prev_nonempty = [ci for ci, c in enumerate(prev_row)
                             if (c or '').strip()]
            prev_c0 = (prev_row[0] if prev_row else '') or ''
            # If previous row has col0 empty and single cell in low col = heading
            if (not prev_c0.strip() and len(prev_nonempty) == 1
                    and prev_nonempty[0] <= 1):
                data_start -= 1
            else:
                break

    # Collect data rows: for each row take non-empty cells in order
    # (column indices vary between pages due to merged cells)
    rows_data = []
    for row in tdata[data_start:]:
        if not row:
            continue
        # Get non-empty cells in column order
        cells = [clean(c) for c in (row or []) if (c or '').strip()]
        if not cells:
            continue
        col0_empty = not (row[0] or '').strip()
        # Sub-group heading: col0 empty, exactly one non-empty cell
        # in a LOW column index (col1), with multiple words
        # Wrap-around continuation rows have content in HIGH col indices
        _nonempty_cols = [ci for ci, c in enumerate(row or []) if (c or '').strip()]
        _is_heading = (col0_empty and len(cells) == 1 and
                       len(cells[0].split()) >= 1 and
                       _nonempty_cols and _nonempty_cols[0] <= 1)
        if _is_heading:
            rows_data.append(('__heading__', cells[0]))
        # Multi-row indication: col0 empty, append to prev row's last col
        # Use <br> separator to preserve line structure in HTML
        elif col0_empty and rows_data:
            last = list(rows_data[-1])
            if last[0] != '__heading__':
                suffix = ' '.join(cells)
                last[-1] = (last[-1] + '<br>' + suffix).strip('<br>')
                rows_data[-1] = last
        else:
            # Pad or trim to 3 columns
            while len(cells) < 3:
                cells.append('')
            rows_data.append(cells[:3])

    if not rows_data:
        return ''

    lines = []
    lines.append('  <table border="1" bordercolor="#000000" cellpadding="4" '
                 'cellspacing="0" style="width:100%;font-size:9pt">')

    if is_header_page:
        lines.append('   <thead>')
        lines.append('    <tr style="background:#e8e8e8">')
        for h in headers:
            lines.append(f'     <th>{esc(h)}</th>')
        lines.append('    </tr>')
        lines.append('   </thead>')

    lines.append('   <tbody>')
    for cells in rows_data:
        if cells[0] == '__heading__':
            lines.append('    <tr style="background:#e8e8e8">')
            lines.append(f'     <td colspan="3" style="font-weight:bold">'
                         f'{esc(cells[1])}</td>')
            lines.append('    </tr>')
        else:
            lines.append('    <tr>')
            for ci, c in enumerate(cells):
                # Last column may contain <br> for wrapped rows — don't escape
                if '<br>' in c:
                    lines.append(f'     <td style="vertical-align:top">{c}</td>')
                else:
                    lines.append(f'     <td style="vertical-align:top">{esc(c)}</td>')
            lines.append('    </tr>')
    lines.append('   </tbody>')
    lines.append('  </table>')
    return '\n'.join(lines)


def reconstruct_fda_dosing_table(p1_data, p2_data=None, gray_bottom=None,
                                  p1_page=None, p2_page=None):
    """
    Reconstruct a 2-column FDA-Approved Dosing table that spans two pages.

    p1_page, p2_page: pdfplumber Page objects for underline detection.
    """
    from html import escape as esc

    def get_underline_rects(page):
        if page is None:
            return []
        return [r for r in page.rects
                if (r['bottom'] - r['top'] < 2 and
                    r['x0'] >= 135 and r['x1'] < 520)]

    def build_cell_html(page, x0_min, x1_max, y_top, y_bot, underline_rects):
        """Extract chars from page in given bbox, build HTML with <u> spans."""
        if page is None:
            return ''
        chars = [c for c in page.chars
                 if c['x0'] >= x0_min - 2 and c['x1'] <= x1_max + 2
                 and c['top'] >= y_top - 2 and c['bottom'] <= y_bot + 2]
        if not chars:
            return ''
        chars.sort(key=lambda c: (round(c['top'] / 2) * 2, c['x0']))

        def is_underlined(ch):
            return any(
                r['top'] >= ch['top'] and
                r['top'] <= ch['bottom'] + 2 and
                r['x0'] <= ch['x0'] and
                ch['x1'] <= r['x1'] + 2
                for r in underline_rects
            )

        # Group chars into lines by y
        from itertools import groupby
        result_parts = []
        for _, line_chars in groupby(chars,
                                     key=lambda c: round(c['top'] / 2) * 2):
            line_chars = list(sorted(line_chars, key=lambda c: c['x0']))
            # Build spans: underlined vs non-underlined
            line_html = ''
            in_ul = False
            for ch in line_chars:
                ul = is_underlined(ch)
                if ul and not in_ul:
                    line_html += '<u>'
                    in_ul = True
                elif not ul and in_ul:
                    line_html += '</u>'
                    in_ul = False
                line_html += esc(ch['text'])
            if in_ul:
                line_html += '</u>'
            result_parts.append(line_html)

        # Join lines: use <br> before lines starting with bullet,
        # otherwise space
        if not result_parts:
            return ''
        result = result_parts[0]
        for part in result_parts[1:]:
            stripped = part.lstrip()
            if stripped.startswith('•') or stripped.startswith('\u2022'):
                result += '<br>' + part
            else:
                result += ' ' + part
        return result.strip()

    if not p1_data:
        return ''

    # ── Detect header rows via gray_bottom ───────────────────────────────
    data_start = 1
    if gray_bottom is not None:
        for ri, row in enumerate(p1_data):
            c0 = (row[0] if row else None) or ''
            others = [(row[ci] or '').strip() for ci in range(1, len(row))]
            if c0.strip() or not any(others):
                data_start = ri
                break
        else:
            data_start = len(p1_data)

    # ── Collapse header rows into two column labels ───────────────────────
    hdr_col1_parts = []
    hdr_col2_parts = []
    for ri in range(data_start):
        row = p1_data[ri]
        c1 = (row[1] if len(row) > 1 else None) or ''
        last = next((row[ci] for ci in range(len(row)-1, 1, -1)
                     if (row[ci] or '').strip()), '')
        if c1.strip():
            hdr_col1_parts.append(c1.strip())
        if last.strip() and last != c1:
            hdr_col2_parts.append(last.strip())

    col1_hdr = ' '.join(hdr_col1_parts) or 'FDA-Approved Indication'
    col2_hdr = ' '.join(hdr_col2_parts) or 'Dosing'

    # ── Get underline rects for each page ────────────────────────────────
    ul_rects_p1 = get_underline_rects(p1_page)
    ul_rects_p2 = get_underline_rects(p2_page)

    # ── Determine table x-bounds for indication vs dosing columns ────────
    # Page 1: indication in col0 (x~54-134), dosing in last col (x~135-526)
    # Page 2: indication in col0 (x~54-134), dosing in col1 (x~135-526)
    IND_X0, IND_X1 = 54.0, 135.0
    DOS_X0, DOS_X1 = 135.0, 527.0

    # ── Collect rows using char-level extraction ──────────────────────────
    # We need bbox per pdfplumber row to extract chars correctly.
    # Use pdfplumber's row y-ranges from the table object if available,
    # otherwise fall back to cell text.

    def _cell_text(cell):
        return (cell or '').strip()

    rows = []  # list of (indication_html, dosing_html)

    # Page 1 data rows
    if p1_page is not None and p1_data:
        # Get table bbox from page
        tables = p1_page.find_tables()
        if tables:
            t = tables[0]
            trows = t.rows

            # Pre-compute sub-row y_tops per parent row to clip parent extraction
            # A sub-row has x0 >= DOS_X0 (no indication column)
            sub_row_tops = {}  # parent_ri -> list of sub-row y_tops
            for ri, trow in enumerate(trows):
                if trow.bbox[0] >= DOS_X0 - 5:  # sub-row: starts in dosing col
                    # Find parent: last row that starts at IND_X0
                    for pri in range(ri - 1, -1, -1):
                        if trows[pri].bbox[0] < DOS_X0 - 5:
                            sub_row_tops.setdefault(pri, []).append(trow.bbox[1])
                            break

            for ri, trow in enumerate(trows):
                if ri < data_start:
                    continue
                y_top = trow.bbox[1]
                y_bot = trow.bbox[3]
                is_subrow = trow.bbox[0] >= DOS_X0 - 5
                if not is_subrow:
                    # Clip y_bot to first sub-row top if any
                    if ri in sub_row_tops:
                        y_bot = min(y_bot, min(sub_row_tops[ri]))
                    ind_html = build_cell_html(p2_page, IND_X0, IND_X1,
                                               y_top, y_bot, ul_rects_p2)
                    dos_html = build_cell_html(p2_page, DOS_X0, DOS_X1,
                                               y_top, y_bot, ul_rects_p2)
                else:
                    ind_html = ''
                    dos_html = build_cell_html(p2_page, DOS_X0, DOS_X1,
                                               y_top, y_bot, ul_rects_p2)

                if not ind_html and not dos_html:
                    continue
                if not ind_html:
                    # Sub-row: append to previous
                    if rows:
                        rows[-1] = (rows[-1][0],
                                    rows[-1][1] + '<br>' + dos_html)
                else:
                    rows.append((ind_html, dos_html))
    else:
        # Fallback: use cell text without underline
        for row in p1_data[data_start:]:
            ind = _cell_text(row[0] if len(row) > 0 else '')
            dos = _cell_text(next(
                ((row[ci] or '') for ci in range(len(row)-1, 0, -1)
                 if (row[ci] or '').strip()), ''))
            if not ind and not dos:
                continue
            if not ind:
                if rows:
                    rows[-1] = (rows[-1][0],
                                rows[-1][1] + '<br>' + esc(dos))
            else:
                rows.append((esc(ind), esc(dos)))

    # Page 2 data rows
    if p2_page is not None and p2_data:
        tables = p2_page.find_tables()
        if tables:
            t = tables[0]
            trows = t.rows

            # Pre-compute sub-row y_tops per parent row to clip parent extraction
            # A sub-row has x0 >= DOS_X0 (no indication column)
            sub_row_tops = {}  # parent_ri -> list of sub-row y_tops
            for ri, trow in enumerate(trows):
                if trow.bbox[0] >= DOS_X0 - 5:  # sub-row: starts in dosing col
                    # Find parent: last row that starts at IND_X0
                    for pri in range(ri - 1, -1, -1):
                        if trows[pri].bbox[0] < DOS_X0 - 5:
                            sub_row_tops.setdefault(pri, []).append(trow.bbox[1])
                            break

            for ri, trow in enumerate(trows):
                y_top = trow.bbox[1]
                y_bot = trow.bbox[3]
                is_subrow = trow.bbox[0] >= DOS_X0 - 5
                if not is_subrow:
                    # Clip y_bot to first sub-row top if any
                    if ri in sub_row_tops:
                        y_bot = min(y_bot, min(sub_row_tops[ri]))

                    ind_html = build_cell_html(p2_page, IND_X0, IND_X1,
                                               y_top, y_bot, ul_rects_p2)
                    dos_html = build_cell_html(p2_page, DOS_X0, DOS_X1,
                                               y_top, y_bot, ul_rects_p2)
                else:
                    ind_html = ''
                    dos_html = build_cell_html(p2_page, DOS_X0, DOS_X1,
                                               y_top, y_bot, ul_rects_p2)

                if not ind_html and not dos_html:
                    continue
                if not ind_html:
                    if rows:
                        rows[-1] = (rows[-1][0],
                                    rows[-1][1] + '<br>' + dos_html)
                else:
                    rows.append((ind_html, dos_html))
    elif p2_data:
        for row in p2_data:
            if not row:
                continue
            ind = _cell_text(row[0] or '')
            dos = _cell_text(row[1] if len(row) > 1 else '')
            if not ind and not dos:
                continue
            if not ind:
                if rows:
                    rows[-1] = (rows[-1][0],
                                rows[-1][1] + '<br>' + esc(dos))
            else:
                rows.append((esc(ind), esc(dos)))

    if not rows:
        return ''

    # ── Render ───────────────────────────────────────────────────────────
    lines = [
        '  <table border="1" bordercolor="#000000" cellpadding="4" '
        'cellspacing="0" style="width:100%;font-size:9pt">',
        '   <thead>',
        '    <tr style="background:#e8e8e8">',
        f'     <th>{esc(col1_hdr)}</th>',
        f'     <th>{esc(col2_hdr)}</th>',
        '    </tr>',
        '   </thead>',
        '   <tbody>',
    ]
    for ind_html, dos_html in rows:
        lines.append('    <tr>')
        lines.append(f'     <td style="vertical-align:top">{ind_html}</td>')
        lines.append(f'     <td style="vertical-align:top">{dos_html}</td>')
        lines.append('    </tr>')
    lines += ['   </tbody>', '  </table>']
    return '\n'.join(lines)
