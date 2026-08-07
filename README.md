# VMM Python snapshot benchmark

This Windows/WHP benchmark compares Python cold starts and persisted snapshot
resumes. It records direct-process samples, wall-clock execution time, and
Windows peak resident memory (`PeakWorkingSetSize`). Hyperlight also emits
runner-internal lifecycle timers, which are summarized separately from
end-to-end process latency.

The three benchmark targets are:

- `nvx`: Python on NVX/OpenVMM/Linux
- `nanvix`: Python on Nanvix/OpenVMM
- `hyperlight`: Python on Hyperlight/Unikraft

## Workloads

The default `hello` workload runs the exact program in `samples/hello.py` on
NVX, Nanvix, and Hyperlight. Benchmark preparation copies it into the NVX and
Nanvix host mounts. The Hyperlight runner reads the same file and passes its
source to the initialized CPython driver's `run` function.

The NVX-only `pandoc-docx` workload runs `samples/pandoc_docx.py`. It calls
`pypandoc.convert_text` with in-memory Markdown and writes a DOCX under `/tmp`.
The script prints its success marker when conversion returns; it does not open
or inspect the generated DOCX in the measured process. `pypandoc` is a thin
Python wrapper and still launches the packaged Pandoc executable internally;
the benchmark script does not call `subprocess` itself.

The standard NVX Python initramfs includes NumPy, Pandas, Alpine 3.24's Pandoc
3.10, and `pypandoc` 1.17. During the build, the harness downloads the
platform-independent `pypandoc` wheel through the host Python package index,
verifies its pinned SHA-256, temporarily stages it in the NVX Docker context,
and removes it afterward.

Samples are not packaged in a kernel, initrd, or snapshot. Their SHA-256 hashes
and delivery methods are recorded in `manifest.json`. Use a new output
directory after changing a sample so resumed results cannot mix workloads.

Every baseline compiles and executes the generic Python statement `pass`
immediately before snapshot capture. Hyperlight captures its snapshot after
this call initializes CPython; the snapshot remains workload-independent. NVX
and Nanvix perform the same generic execution warmup before their existing
capture points. For `pandoc-docx`, NVX also warm-imports NumPy, Pandas, and
`pypandoc` before capture; the workload source remains outside the snapshot.

## Prerequisites

- Windows with WHP enabled
- Git for Windows
- Docker running
- Microsoft Edge (used for SVG-to-PNG rendering)
- Python 3.12+ with pip
- Rust/Cargo 1.89+

## Submodules

NVX, Nanvix Python, and Hyperlight/Unikraft are pinned as submodules in this
repository. Clone with `--recurse-submodules`, or initialize an existing clone
from the repository root:

```powershell
git submodule update --init --recursive
```

### Build patches

The pinned NVX and Nanvix revisions need benchmark-specific guest changes. The
NVX patch adds Pandoc and the pinned `pypandoc` wheel to its full Python image,
adds the warm import, and executes the generic Python statement `pass` before
capture. The Nanvix patch adds the same generic statement. The root repository
owns those changes as:

- `patches/nvx-generic-snapshot-warmup.patch`
- `patches/nanvix-generic-snapshot-warmup.patch`

With `--build`, the harness checks whether each patch is absent or already
applied, applies it only when needed, builds the guest artifacts, and reverses
only patches it applied. This keeps the pinned submodule worktrees clean after
successful or failed builds and preserves pre-existing developer changes.
After a successful build, an ignored `.build-receipts/<target>.json` binds the
patch or image provenance to the generated VMM, kernel, and guest artifact
hashes. Snapshot preparation rejects missing or stale receipts instead of
silently labeling an old artifact with the current methodology.

Hyperlight needs no source patch. The harness pulls the versioned
`python-agent-driver-{kernel,initrd}:v0.12.1` images directly from GHCR and
extracts their artifacts before compiling the root-owned runner.

## Reproduce

From the repository root:

```powershell
python .\benchmark.py --build --samples 100
```

To build and run 100 NVX Pandoc Markdown-to-DOCX samples per mode:

```powershell
python .\benchmark.py --build --workload pandoc-docx --samples 100 --output .\results\nvx-pandoc-docx
```

`pandoc-docx` supports only NVX; selecting Nanvix or Hyperlight for that
workload is rejected before building.

`--build` builds each selected runtime and VMM using its documented Windows
flow plus the temporary benchmark patches described above. Omit it to reuse
artifacts from a prior successful harness build with matching receipts. Use
`--output .\results\<run>` to choose or resume a result directory; completed
`(target, mode, sample)` rows in `raw.csv` are skipped. Result directories
created with an older raw CSV schema remain analyzable but cannot be resumed;
select a new output directory.

To prepare snapshots without sampling:

```powershell
python .\benchmark.py --prepare-only --output .\results\prepared
```

To regenerate summaries and plots:

```powershell
python .\benchmark.py --analyze-only --samples 100 --output .\results\<run>
```

Each result directory contains:

- `raw.csv`: all per-process samples
- `summary.csv` and `summary.json`: descriptive statistics
- `phase_summary.csv` and `phase_summary.json`: separate internal phase statistics
- `cdf_execution_time_<workload>_{cold,restore}.{svg,png}`
- `barplot_peak_rss_<workload>_{cold,restore}.{svg,png}`
- `report.md`: benchmark table, revisions, plots, and method
- `metadata.json`: host, tool, repository, and artifact identity
- `manifest.json`: exact direct commands and guest memory sizes

Each peak-RSS figure places all comparable VMM bars on one plotting area and
uses P99 bars with a zero-based linear y-axis sized to the values displayed in
that plot. Execution-time figures are empirical CDFs with shared x-axes
starting at zero. CDF legends follow the curves from left to right at the
median. NVX is blue, Nanvix is green, and Hyperlight is red in every plot.

Cold start creates a fresh process from kernel/initrd and does not load a
persisted snapshot; it does not mean cold host filesystem caches. Hyperlight's
build/evolve API still captures and rewinds an in-memory post-evolve snapshot
before the first guest call, and the report exposes those phases separately.
One unrecorded preflight per new target/mode warms host caches. Runs are
sequential and deterministically randomized.

NVX uses 1024 MiB so its Pandoc-enabled base initramfs can unpack. Nanvix uses
256 MiB, and the Hyperlight CPython driver uses 2560 MiB. Every run records
SHA-256 hashes for the sample, kernel, initrd, and each selected VMM executable
in `metadata.json`.
