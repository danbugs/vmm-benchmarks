from pathlib import Path

import pypandoc

MARKDOWN = """# Snapshot Benchmark

Pandoc converts this **Markdown** document into DOCX.

- Hyperlight
- NVX

| Metric | Value |
|---|---:|
| Samples | 100 |
"""

output_path = Path("/tmp/pandoc-benchmark.docx")
output_path.unlink(missing_ok=True)

result = pypandoc.convert_text(
    MARKDOWN,
    to="docx",
    format="gfm",
    outputfile=str(output_path),
)
assert result == ""

print("PANDOC_DOCX_OK", flush=True)
