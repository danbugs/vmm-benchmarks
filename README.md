# VMM workload snapshot benchmark

This Windows/WHP benchmark compares end-to-end snapshot generation, persisted
snapshot resumes, separately labeled runtime-preinitialized resumes, and warm
reuse. It records direct-process samples, wall-clock execution time, and
Windows peak resident memory (`PeakWorkingSetSize`). Runtime-internal lifecycle
timers are summarized separately from end-to-end process latency.

The three benchmark targets are:

- `nvx`: Linux workloads on NVX/OpenVMM
- `nanvix`: Python on Nanvix/OpenVMM
- `hyperlight`: Unikraft workloads on Hyperlight

## Workloads

| Workload | NVX | Nanvix | Hyperlight |
|---|:---:|:---:|:---:|
| Python hello | Yes | Yes | Yes |
| Python stdlib Markdown-to-DOCX | Yes | No | Yes |
| Python + pypandoc Markdown-to-DOCX | Yes | No | No |
| Native Pandoc Markdown-to-DOCX | Yes | No | Yes |
| Node.js hello | Yes | No | Yes |

Python workloads use the same files under `samples/` on every applicable
target. NVX receives them through a host mount; Hyperlight passes their source
to the initialized CPython driver's `run` function. NVX also receives mounted
shell drivers for native Pandoc and Node.js, while the corresponding Hyperlight
workloads are embedded in their workload images. Hyperlight invokes
`/bin/run-pandoc.sh` and `/app/hello.js` explicitly and requires their workload
markers, so an ELF-loader call without the embedded workload cannot be recorded
as a successful sample.

NVX Node.js also has a distinct `runtime-preinitialized` mode. It restores a
dedicated 512 MiB snapshot containing initialized V8, then receives
`samples/nodejs_hello.js` over virtio-console with fresh entropy and realtime.
The JavaScript is absent from the snapshot. This result remains separate from
ordinary restore because its capture point excludes runtime initialization.

The NVX workload initramfs includes NumPy, Pandas, Alpine 3.24's Pandoc 3.10,
Node.js, and `pypandoc` 1.17. During the build, the harness downloads the
platform-independent `pypandoc` wheel, verifies its pinned SHA-256, temporarily
stages it in the NVX Docker context, and removes it afterward.

Host-provided samples are absent from persisted snapshots. Their SHA-256 hashes
and delivery methods are recorded in `manifest.json`. Python snapshots execute
the generic statement `pass` before capture; `pandoc-docx` additionally
warm-imports NumPy, Pandas, and `pypandoc`. NVX native snapshots are captured
immediately before the host mount is attached and the command is executed.

Warm reuse loads one persisted snapshot per VMM process. Hyperlight restores
its in-memory sandbox between calls. NVX keeps the snapshot-initialized CPython
parent alive and runs every sample in a forked child, isolating interpreter
state while retaining one restored VM. Warm latency includes both the guest
call and the runtime-specific isolation/reset operation. Peak RSS is recorded
once per warm process batch rather than duplicated for every call.

Each `snapshot-generation` sample starts from kernel/initrd, performs the
configured workload-independent warmup, captures VM state, persists it, and
exits. It writes a dedicated scratch snapshot rather than the reusable restore
snapshot. The harness removes the scratch artifact before starting each timer,
so cleanup is excluded from the measurement, validates the generated files,
then removes the scratch artifact after the process exits.

Snapshot generation records one sample per VMM/workload baseline. `--samples`
controls persisted-resume, runtime-preinitialized, and warm-reuse samples.

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
NVX patch adds Pandoc, Node.js, the pinned `pypandoc` wheel, Python warm reuse,
the warm import, and the generic `pass` snapshot warmup. The Nanvix patch adds
the same generic statement. The root repository owns those changes as:

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

Hyperlight needs no source patch. The harness pulls each workload's kernel and
initrd from its configured GHCR image before invoking the root-owned runner.

## Reproduce

From the repository root:

```powershell
python .\benchmark.py --build --samples 100
```

To build and run one NVX snapshot-generation sample plus 100 resume/warm
Markdown-to-DOCX samples:

```powershell
python .\benchmark.py --build --workload pandoc-docx --samples 100 --output .\results\nvx-pandoc-docx
```

`pandoc-docx` supports only NVX; selecting Nanvix or Hyperlight for that
workload is rejected before building.

To compare NVX and Hyperlight on native Pandoc or Node.js:

```powershell
python .\benchmark.py --build --workload pandoc-native --samples 100 --output .\results\pandoc-native
python .\benchmark.py --build --workload nodejs-hello --samples 100 --output .\results\nodejs-hello
```

To run every NVX/Hyperlight workload with one snapshot-generation sample and
100 samples per other available mode:

```powershell
python .\benchmark.py --build --workload hello --vmm nvx --vmm hyperlight --samples 100 --output .\results\all-hello
python .\benchmark.py --workload pandoc-docx-stdlib --vmm nvx --vmm hyperlight --samples 100 --output .\results\all-pandoc-docx-stdlib
python .\benchmark.py --workload pandoc-docx --vmm nvx --samples 100 --output .\results\all-pandoc-docx
python .\benchmark.py --workload pandoc-native --vmm nvx --vmm hyperlight --samples 100 --output .\results\all-pandoc-native
python .\benchmark.py --workload nodejs-hello --vmm nvx --vmm hyperlight --samples 100 --output .\results\all-nodejs-hello
```

`--build` builds each selected runtime and VMM using its documented Windows
flow plus the temporary benchmark patches described above. Omit it to reuse
artifacts from a prior successful harness build with matching receipts. Use
`--output .\results\<run>` to choose or resume a result directory; completed
`(target, mode, sample)` rows in `raw.csv` are skipped. Result directories
created with an older raw CSV or manifest schema remain analyzable but cannot
be resumed; select a new output directory.

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
- `cdf_execution_time_<workload>_{snapshot-generation,restore,runtime-preinitialized,warm}.{svg,png}` for available modes
- `barplot_peak_rss_<workload>_{snapshot-generation,restore,runtime-preinitialized,warm}.{svg,png}` for available modes
- `report.md`: benchmark table, revisions, plots, and method
- `metadata.json`: host, tool, repository, and artifact identity
- `manifest.json`: exact direct commands, per-VMM workload provenance, and guest memory sizes

Each peak-RSS figure places all comparable VMM bars on one plotting area and
uses P99 bars with a zero-based linear y-axis sized to the values displayed in
that plot. Execution-time figures are empirical CDFs with shared x-axes
starting at zero. CDF legends follow the curves from left to right at the
median. NVX is blue, Nanvix is green, and Hyperlight is red in every plot.

NVX snapshot generation includes kernel/initrd boot, workload-independent
warmup, VM-state capture, and snapshot persistence. Hyperlight reports its
sandbox build/evolve, initial rewind, optional Python warmup, in-memory capture,
and OCI snapshot persistence phases separately. One unrecorded preflight per
new target/mode warms host caches. Runs are sequential and deterministically
randomized.

NVX uses 1536 MiB for ordinary modes so its full workload initramfs can unpack;
its runtime-preinitialized Node.js mode uses a dedicated 512 MiB image. Nanvix
uses 256 MiB. Hyperlight uses 2560 MiB for Python and Pandoc workloads and 512
MiB for Node.js. Every run records SHA-256 hashes for the sample, kernel,
initrd, and each selected VMM executable in `metadata.json`.
