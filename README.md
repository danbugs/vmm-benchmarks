# VMM hello-world benchmark

This Windows/WHP benchmark compares Python `hello.py` cold starts and persisted
snapshot resumes on NVX, Nanvix, and Hyperlight. It records 100 direct-process
samples per target/mode, wall-clock execution time, and Windows peak resident
memory (`PeakWorkingSetSize`). Hyperlight also emits runner-internal lifecycle
timers, which are summarized separately from end-to-end process latency.

The three benchmark targets are:

- `nvx`: Python on NVX/OpenVMM/Linux
- `nanvix`: Python on Nanvix/OpenVMM
- `hyperlight`: Python on Hyperlight/Unikraft

## Sample program

All targets run the exact program in `samples/hello.py`. Benchmark preparation
copies it into the NVX and Nanvix host mounts. The Hyperlight runner reads the
same file and passes its source to the initialized CPython driver's `run`
function. The sample is not packaged in the Hyperlight kernel, initrd, or
snapshot. Its SHA-256 hash and delivery method are recorded in `manifest.json`.
Use a new output directory after changing the sample so resumed results cannot
mix different workloads.

Every baseline compiles and executes the generic Python statement `pass`
immediately before snapshot capture. Hyperlight captures its snapshot after
this call initializes CPython; the snapshot remains workload-independent. NVX
and Nanvix perform the same generic execution warmup before their existing
capture points.

## Prerequisites

- Windows with WHP enabled
- Git for Windows
- Docker running
- Microsoft Edge (used for SVG-to-PNG rendering)
- Python 3.12+
- Rust/Cargo 1.89+

## Submodules

NVX, Nanvix Python, and Hyperlight/Unikraft are pinned as submodules in this
repository. Clone with `--recurse-submodules`, or initialize an existing clone
from the repository root:

```powershell
git submodule update --init --recursive
```

### Build patches

The pinned NVX and Nanvix revisions need one benchmark-specific guest change
each so the generic Python statement `pass` executes immediately before
snapshot capture. The root repository owns those changes as:

- `patches/nvx-generic-snapshot-warmup.patch`
- `patches/nanvix-generic-snapshot-warmup.patch`

With `--build`, the harness checks whether each patch is absent or already
applied, applies it only when needed, builds the guest artifacts, and reverses
only patches it applied. This keeps the pinned submodule worktrees clean after
successful or failed builds and preserves pre-existing developer changes.
After a successful build, an ignored `.build-receipts/<target>.json` binds the
patch or image provenance to the generated artifact hashes. Snapshot preparation
rejects missing or stale receipts instead of silently labeling an old artifact
with the current methodology.

Hyperlight needs no source patch. The harness pulls the versioned
`python-agent-driver-{kernel,initrd}:v0.12.1` images directly from GHCR and
extracts their artifacts before compiling the root-owned runner.

## Reproduce

From the repository root:

```powershell
python .\benchmark.py --build --samples 100
```

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
- `cdf_execution_time_python_{cold,restore}.{svg,png}`
- `barplot_peak_rss_python_{cold,restore}.{svg,png}`
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

NVX requires 512 MiB, Nanvix uses 256 MiB, and the Hyperlight CPython driver
uses 2560 MiB. Every run records SHA-256 hashes for the kernel, initrd, and each
VMM executable in `metadata.json`.
