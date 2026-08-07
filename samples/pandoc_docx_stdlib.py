"""Markdown-to-DOCX benchmark — pure Python, no external binaries.

Converts Markdown to DOCX using only the standard library
(zipfile + xml.etree.ElementTree). Same input markdown and output
format as the pypandoc variant so the workload is comparable.
"""

import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

MARKDOWN = """# Snapshot Benchmark

Pandoc converts this **Markdown** document into DOCX.

- Hyperlight
- NVX

| Metric | Value |
|---|---:|
| Samples | 100 |
"""

# ── OOXML constants ──────────────────────────────────────────────────────

CONTENT_TYPES_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", W)

def _w(tag):
    return f"{{{W}}}{tag}"


# ── Markdown parser ──────────────────────────────────────────────────────

def parse_markdown(text):
    blocks = []
    lines = text.split("\n")
    i = 0
    table_rows = []

    def flush_table():
        nonlocal table_rows
        if table_rows:
            blocks.append({"type": "table", "rows": table_rows})
            table_rows = []

    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_table()
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2)})
            i += 1
            continue
        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                i += 1
                continue
            table_rows.append(cells)
            i += 1
            continue
        m = re.match(r"^[-*]\s+(.*)", line)
        if m:
            flush_table()
            blocks.append({"type": "list_item", "text": m.group(1)})
            i += 1
            continue
        if not line.strip():
            flush_table()
            i += 1
            continue
        flush_table()
        para_lines = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith("#") and not lines[i].strip().startswith("|"):
            para_lines.append(lines[i])
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(para_lines)})
    flush_table()
    return blocks


def parse_inline(text):
    runs = []
    pattern = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False, False))
        if m.group(1):
            runs.append((m.group(1), True, False))
        elif m.group(2):
            runs.append((m.group(2), False, True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False, False))
    return runs


# ── DOCX builder ─────────────────────────────────────────────────────────

def make_paragraph(text, style=None):
    p = ET.Element(_w("p"))
    if style:
        ppr = ET.SubElement(p, _w("pPr"))
        pstyle = ET.SubElement(ppr, _w("pStyle"))
        pstyle.set(_w("val"), style)
    for chunk, bold, italic in parse_inline(text):
        r = ET.SubElement(p, _w("r"))
        if bold or italic:
            rpr = ET.SubElement(r, _w("rPr"))
            if bold:
                ET.SubElement(rpr, _w("b"))
            if italic:
                ET.SubElement(rpr, _w("i"))
        t = ET.SubElement(r, _w("t"))
        t.text = chunk
        t.set("xml:space", "preserve")
    return p


def make_table(rows):
    tbl = ET.Element(_w("tbl"))
    tbl_pr = ET.SubElement(tbl, _w("tblPr"))
    borders = ET.SubElement(tbl_pr, _w("tblBorders"))
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = ET.SubElement(borders, _w(side))
        b.set(_w("val"), "single")
        b.set(_w("sz"), "4")
        b.set(_w("space"), "0")
        b.set(_w("color"), "auto")
    for row_cells in rows:
        tr = ET.SubElement(tbl, _w("tr"))
        for cell_text in row_cells:
            tc = ET.SubElement(tr, _w("tc"))
            tc.append(make_paragraph(cell_text.strip()))
    return tbl


def blocks_to_docx(blocks):
    body = ET.Element(_w("body"))
    for block in blocks:
        if block["type"] == "heading":
            body.append(make_paragraph(block["text"], style=f"Heading{block['level']}"))
        elif block["type"] == "paragraph":
            body.append(make_paragraph(block["text"]))
        elif block["type"] == "list_item":
            body.append(make_paragraph(block["text"], style="ListParagraph"))
        elif block["type"] == "table":
            body.append(make_table(block["rows"]))
    doc = ET.Element(_w("document"))
    doc.append(body)
    return doc


def write_docx(doc_element, path):
    buf = BytesIO()
    tree = ET.ElementTree(doc_element)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", RELS_XML)
        doc_bytes = BytesIO()
        tree.write(doc_bytes, xml_declaration=True, encoding="UTF-8")
        zf.writestr("word/document.xml", doc_bytes.getvalue())
    Path(path).write_bytes(buf.getvalue())
    return len(buf.getvalue())


# ── Main ─────────────────────────────────────────────────────────────────

output_path = "/tmp/pandoc-benchmark.docx"
blocks = parse_markdown(MARKDOWN)
doc = blocks_to_docx(blocks)
size = write_docx(doc, output_path)
print(f"Generated {size} bytes DOCX with {len(blocks)} blocks", flush=True)
print("PANDOC_DOCX_OK", flush=True)
