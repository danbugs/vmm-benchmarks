#!/bin/sh
set -eu

input=/tmp/vmm-benchmark.md
output=/tmp/vmm-benchmark.docx

cat > "$input" <<'EOF'
# Snapshot Benchmark

Pandoc converts this **Markdown** document into DOCX.

- Hyperlight
- NVX

| Metric | Value |
|---|---:|
| Samples | 100 |
EOF

pandoc --from=markdown --to=docx --output="$output" "$input"
test -s "$output"
printf 'PANDOC_DOCX_OK\n'
