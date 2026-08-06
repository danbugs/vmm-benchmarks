#!/usr/bin/env python3
"""Benchmark Python hello-world cold starts and persisted snapshot resumes."""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import html
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterable, Sequence, cast

from ctypes import wintypes


SCRIPT_DIR = Path(__file__).resolve().parent
NVX_DIR = SCRIPT_DIR / "nvx"
NANVIX_DIR = SCRIPT_DIR / "nanvix-python"
HYPERLIGHT_DIR = SCRIPT_DIR / "hyperlight-unikraft"
HYPERLIGHT_RUNNER_DIR = SCRIPT_DIR / "hyperlight-runner"
PATCHES_DIR = SCRIPT_DIR / "patches"
BUILD_RECEIPTS_DIR = SCRIPT_DIR / ".build-receipts"
SAMPLES_DIR = SCRIPT_DIR / "samples"
HELLO_SAMPLE = SAMPLES_DIR / "hello.py"
NVX_BUILD_PATCH = PATCHES_DIR / "nvx-generic-snapshot-warmup.patch"
NANVIX_BUILD_PATCH = PATCHES_DIR / "nanvix-generic-snapshot-warmup.patch"
HYPERLIGHT_DRIVER_TAG = "v0.12.1"
HYPERLIGHT_DRIVER_KERNEL_IMAGE = (
    "ghcr.io/hyperlight-dev/hyperlight-unikraft/"
    f"python-agent-driver-kernel:{HYPERLIGHT_DRIVER_TAG}"
)
HYPERLIGHT_DRIVER_INITRD_IMAGE = (
    "ghcr.io/hyperlight-dev/hyperlight-unikraft/"
    f"python-agent-driver-initrd:{HYPERLIGHT_DRIVER_TAG}"
)
HYPERLIGHT_DRIVER_DIR = HYPERLIGHT_DIR / "examples" / "python-agent-driver"
HYPERLIGHT_DRIVER_KERNEL = (
    HYPERLIGHT_DRIVER_DIR
    / ".unikraft"
    / "build"
    / "python-agent-driver-hyperlight_hyperlight-x86_64"
)
HYPERLIGHT_DRIVER_INITRD = HYPERLIGHT_DRIVER_DIR / "python-agent-driver-initrd.cpio"

VMM_ORDER = ("nvx", "nanvix", "hyperlight")
VMM_LABELS = {
    "nvx": "NVX (OpenVMM + Linux)",
    "nanvix": "Nanvix (OpenVMM + Nanvix)",
    "hyperlight": "Hyperlight (Hyperlight + Unikraft)",
}
VMM_PLOT_LABELS = dict(VMM_LABELS)
VMM_PLOT_COLORS = {
    "nvx": "#2563eb",
    "nanvix": "#16a34a",
    "hyperlight": "#dc2626",
}
WORKLOAD_LABELS = {
    "nvx": "Python hello.py",
    "nanvix": "Python hello.py",
    "hyperlight": "Python hello.py",
}
PLOT_WORKLOAD_ORDER = ("python",)
PLOT_WORKLOAD_LABELS = {"python": "Python"}
PLOT_WORKLOAD_VMMS = {"python": VMM_ORDER}
RSS_VMM_ORDER = ("nanvix", "nvx", "hyperlight")
GUEST_MEMORY_MIB = {
    "nvx": 512,
    "nanvix": 256,
    "hyperlight": 2560,
}
MODE_ORDER = ("cold", "restore")
MODE_LABELS = {"cold": "Cold Start", "restore": "Persisted Snapshot Resume"}
GENERIC_SNAPSHOT_WARMUP = "pass"
HYPERLIGHT_SNAPSHOT_FORMAT = 2
PHASE_FIELDS = (
    "sandbox_build_ms",
    "initial_rewind_ms",
    "snapshot_load_ms",
    "guest_call_ms",
    "lifecycle_overhead_ms",
)
PHASE_LABELS = {
    "sandbox_build_ms": "Sandbox build/evolve",
    "initial_rewind_ms": "Initial in-memory rewind",
    "snapshot_load_ms": "Persisted snapshot load + VM construction",
    "guest_call_ms": "First guest invocation",
    "lifecycle_overhead_ms": "Remaining process lifecycle",
}
NVX_CMDLINE = (
    "earlycon=xe9 console=hvc0 quiet loglevel=0 reboot=t panic=-1 "
    "nvx_mode=hostfs nvx_snapshot=1 pyapp=app.py"
)


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


GET_PROCESS_MEMORY_INFO = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
GET_PROCESS_MEMORY_INFO.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(ProcessMemoryCounters),
    wintypes.DWORD,
]
GET_PROCESS_MEMORY_INFO.restype = wintypes.BOOL


@dataclass(frozen=True)
class CommandSpec:
    vmm: str
    mode: str
    executable: Path
    arguments: tuple[str, ...]
    cwd: Path
    success_marker: str

    @property
    def command(self) -> list[str]:
        return [str(self.executable), *self.arguments]


@dataclass(frozen=True)
class Measurement:
    elapsed_ms: float
    peak_rss_bytes: int
    exit_code: int
    output: str
    phases_ms: dict[str, float]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command_text(command: Sequence[str]) -> str:
    return subprocess.list2cmdline([str(item) for item in command])


def run_checked(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> None:
    rendered = [str(item) for item in command]
    print(f"+ ({cwd}) {command_text(rendered)}", flush=True)
    subprocess.run(rendered, cwd=cwd, env=env, timeout=timeout, check=True)


def require_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required command is not on PATH: {name}")
    return path


def require_file(path: Path) -> Path:
    if not path.is_file():
        raise RuntimeError(f"required file does not exist: {path}")
    return path


def build_receipt_payload(
    provenance: dict[str, str],
    artifacts: dict[str, Path],
) -> dict[str, object]:
    return {
        "format": 1,
        "provenance": provenance,
        "artifacts": {
            name: {
                "path": str(path.relative_to(SCRIPT_DIR)),
                "sha256": sha256(require_file(path)),
            }
            for name, path in artifacts.items()
        },
    }


def write_build_receipt(
    vmm: str,
    provenance: dict[str, str],
    artifacts: dict[str, Path],
) -> None:
    BUILD_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    receipt = BUILD_RECEIPTS_DIR / f"{vmm}.json"
    receipt.write_text(
        json.dumps(build_receipt_payload(provenance, artifacts), indent=2),
        encoding="utf-8",
    )


def require_build_receipt(
    vmm: str,
    provenance: dict[str, str],
    artifacts: dict[str, Path],
) -> None:
    receipt = BUILD_RECEIPTS_DIR / f"{vmm}.json"
    expected = build_receipt_payload(provenance, artifacts)
    actual: object = None
    if receipt.is_file():
        actual = json.loads(receipt.read_text(encoding="utf-8"))
    if actual != expected:
        raise RuntimeError(
            f"{vmm} artifacts were not built with the current benchmark provenance; "
            "rerun with --build"
        )


def git_apply_check(repository: Path, patch: Path, *, reverse: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["git", "apply", "--ignore-space-change", "--check"]
    if reverse:
        command.append("--reverse")
    command.append(str(patch))
    return subprocess.run(
        command,
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )


@contextmanager
def temporary_submodule_patch(
    repository: Path,
    patch: Path,
) -> Generator[None, None, None]:
    patch = require_file(patch)
    applied_here = False
    forward = git_apply_check(repository, patch)
    if forward.returncode == 0:
        run_checked(
            ["git", "apply", "--ignore-space-change", patch],
            cwd=repository,
        )
        applied_here = True
    else:
        reverse = git_apply_check(repository, patch, reverse=True)
        if reverse.returncode != 0:
            raise RuntimeError(
                f"cannot apply build patch {patch} in {repository}\n"
                f"forward check:\n{forward.stdout}\n"
                f"reverse check:\n{reverse.stdout}"
            )
    try:
        yield
    finally:
        if applied_here:
            run_checked(
                ["git", "apply", "--ignore-space-change", "--reverse", patch],
                cwd=repository,
            )


def remove_docker_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        cwd=SCRIPT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def extract_docker_artifact(image: str, source: str, destination: Path) -> None:
    container = f"vmm-benchmark-{destination.stem}-{os.getpid()}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    run_checked(["docker", "pull", image], cwd=SCRIPT_DIR)
    remove_docker_container(container)
    try:
        run_checked(
            ["docker", "create", "--name", container, image, source],
            cwd=SCRIPT_DIR,
        )
        run_checked(
            ["docker", "cp", f"{container}:{source}", destination],
            cwd=SCRIPT_DIR,
        )
    finally:
        remove_docker_container(container)
    require_file(destination)


def stage_sample(output_dir: Path, artifacts: Path) -> Path:
    source = require_file(HELLO_SAMPLE)
    staged = artifacts / "samples" / source.name
    source_bytes = source.read_bytes()
    staged_exists = staged.is_file()
    changed = not staged_exists or staged.read_bytes() != source_bytes
    if changed and (output_dir / "raw.csv").is_file():
        raise RuntimeError(
            f"benchmark sample changed for existing results in {output_dir}; "
            "choose a new --output directory"
        )
    staged.parent.mkdir(parents=True, exist_ok=True)
    if changed:
        shutil.copyfile(source, staged)
    return staged


def build_projects(selected: set[str]) -> None:
    require_command("docker")
    if selected.intersection({"nvx", "hyperlight"}):
        require_command("cargo")

    if "nvx" in selected:
        (BUILD_RECEIPTS_DIR / "nvx.json").unlink(missing_ok=True)
        with temporary_submodule_patch(NVX_DIR, NVX_BUILD_PATCH):
            run_checked(
                [
                    sys.executable,
                    "scripts\\nvx.py",
                    "build-linux-artifacts",
                    "--dest",
                    "build",
                ],
                cwd=NVX_DIR,
            )
            run_checked(
                [
                    sys.executable,
                    "scripts\\nvx.py",
                    "build-python-initramfs",
                    "--docker",
                    "--dest",
                    "build",
                ],
                cwd=NVX_DIR,
            )
            run_checked(["cargo", "build", "--release"], cwd=NVX_DIR)
            write_build_receipt(
                "nvx",
                {"build_patch_sha256": sha256(NVX_BUILD_PATCH)},
                {"python_initrd": NVX_DIR / "build" / "initramfs-python.cpio.gz"},
            )

    if "nanvix" in selected:
        (BUILD_RECEIPTS_DIR / "nanvix.json").unlink(missing_ok=True)
        powershell = require_command("powershell.exe")
        z_script = NANVIX_DIR / "z.ps1"
        with temporary_submodule_patch(NANVIX_DIR, NANVIX_BUILD_PATCH):
            for action in ("setup", "build", "test", "release"):
                run_checked(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        z_script,
                        action,
                    ],
                    cwd=NANVIX_DIR,
                )
            write_build_receipt(
                "nanvix",
                {"build_patch_sha256": sha256(NANVIX_BUILD_PATCH)},
                {"release_zip": nanvix_release_zip()},
            )

    if "hyperlight" in selected:
        (BUILD_RECEIPTS_DIR / "hyperlight.json").unlink(missing_ok=True)
        run_checked(["cargo", "build", "--release"], cwd=HYPERLIGHT_DIR / "host")
        extract_docker_artifact(
            HYPERLIGHT_DRIVER_INITRD_IMAGE,
            "/initrd.cpio",
            HYPERLIGHT_DRIVER_INITRD,
        )
        extract_docker_artifact(
            HYPERLIGHT_DRIVER_KERNEL_IMAGE,
            "/kernel",
            HYPERLIGHT_DRIVER_KERNEL,
        )
        run_checked(["cargo", "build", "--release"], cwd=HYPERLIGHT_RUNNER_DIR)
        write_build_receipt(
            "hyperlight",
            {
                "kernel_image": HYPERLIGHT_DRIVER_KERNEL_IMAGE,
                "initrd_image": HYPERLIGHT_DRIVER_INITRD_IMAGE,
            },
            {
                "kernel": HYPERLIGHT_DRIVER_KERNEL,
                "initrd": HYPERLIGHT_DRIVER_INITRD,
                "runner": HYPERLIGHT_RUNNER_DIR
                / "target"
                / "release"
                / "vmm-hyperlight-runner.exe",
            },
        )


def run_control(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    timeout: float,
    marker: str | None = None,
) -> str:
    rendered = [str(item) for item in command]
    print(f"+ ({cwd}) {command_text(rendered)}", flush=True)
    completed = subprocess.run(
        rendered,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0 or (marker is not None and marker not in output):
        raise RuntimeError(
            f"control command failed ({completed.returncode}): {command_text(rendered)}\n{output}"
        )
    return output


def peak_working_set(process: subprocess.Popen[bytes]) -> int:
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not GET_PROCESS_MEMORY_INFO(
        wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
        ctypes.byref(counters),
        counters.cb,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def measure(spec: CommandSpec, timeout: float) -> Measurement:
    started = time.perf_counter_ns()
    process = subprocess.Popen(
        spec.command,
        cwd=spec.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output_bytes, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        output_bytes, _ = process.communicate()
        raise RuntimeError(
            f"{spec.vmm}/{spec.mode} timed out after {timeout:.1f}s\n"
            f"{output_bytes.decode('utf-8', errors='replace')}"
        )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    peak_rss_bytes = peak_working_set(process)
    output = output_bytes.decode("utf-8", errors="replace")
    phases_ms: dict[str, float] = {}
    for line in output.splitlines():
        if not line.startswith("BENCHMARK_PHASE "):
            continue
        for field in line.removeprefix("BENCHMARK_PHASE ").split():
            name, separator, value = field.partition("=")
            if separator and name in PHASE_FIELDS:
                phases_ms[name] = float(value)
    measured_internal_ms = sum(phases_ms.values())
    if measured_internal_ms:
        phases_ms["lifecycle_overhead_ms"] = elapsed_ms - measured_internal_ms
    return Measurement(
        elapsed_ms,
        peak_rss_bytes,
        process.returncode,
        output,
        phases_ms,
    )


def nanvix_release_zip() -> Path:
    dist = NANVIX_DIR / ".nanvix" / "out" / "dist"
    matches = sorted(
        dist.glob("nanvix-python-windows-x86-microvm-standalone-256mb.zip")
    )
    if len(matches) != 1:
        raise RuntimeError(f"expected one Nanvix release ZIP in {dist}, found {len(matches)}")
    return matches[0]


def prepare(
    output_dir: Path,
    selected: set[str],
    timeout: float,
) -> dict[tuple[str, str], CommandSpec]:
    artifacts = output_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    sample = stage_sample(output_dir, artifacts)
    specs: dict[tuple[str, str], CommandSpec] = {}

    if "nvx" in selected:
        nvx_work = artifacts / "nvx"
        nvx_mount = nvx_work / "mnt"
        nvx_snapshot = nvx_work / "snapshot"
        nvx_mount.mkdir(parents=True, exist_ok=True)
        nvx_snapshot.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample, nvx_mount / "app.py")

        executable = require_file(NVX_DIR / "target" / "release" / "microvm.exe")
        kernel = require_file(NVX_DIR / "build" / "vmlinux")
        initrd = require_file(NVX_DIR / "build" / "initramfs-python.cpio.gz")
        require_build_receipt(
            "nvx",
            {"build_patch_sha256": sha256(NVX_BUILD_PATCH)},
            {"python_initrd": initrd},
        )
        nvx_snapshot_config_path = nvx_snapshot / "benchmark-config.json"
        expected_nvx_snapshot_config: dict[str, object] = {
            "format": 1,
            "kernel_sha256": sha256(kernel),
            "initrd_sha256": sha256(initrd),
            "build_patch_sha256": sha256(NVX_BUILD_PATCH),
            "cmdline": NVX_CMDLINE,
            "warmup": GENERIC_SNAPSHOT_WARMUP,
            "workload_in_snapshot": False,
        }
        nvx_snapshot_config: object = None
        if nvx_snapshot_config_path.is_file():
            nvx_snapshot_config = json.loads(
                nvx_snapshot_config_path.read_text(encoding="utf-8")
            )
        nvx_snapshot_matches_image = (
            (nvx_snapshot / "state.bin").is_file()
            and (nvx_snapshot / "mem.bin").is_file()
            and nvx_snapshot_config == expected_nvx_snapshot_config
        )
        if not nvx_snapshot_matches_image:
            if (output_dir / "raw.csv").is_file():
                raise RuntimeError(
                    "NVX snapshot does not match the current generic CPython image "
                    f"in {output_dir}; choose a new --output directory"
                )
            shutil.rmtree(nvx_snapshot, ignore_errors=True)
            nvx_snapshot.mkdir(parents=True)
            run_control(
                [
                    executable,
                    "--kernel",
                    kernel,
                    "--initrd",
                    initrd,
                    "--mem",
                    "512",
                    "--cmdline",
                    NVX_CMDLINE,
                    "--mount",
                    nvx_mount,
                    "--snapshot",
                    nvx_snapshot,
                    "--quiet",
                ],
                cwd=NVX_DIR,
                timeout=timeout,
            )
            nvx_snapshot_config_path.write_text(
                json.dumps(expected_nvx_snapshot_config, indent=2),
                encoding="utf-8",
            )
        require_file(nvx_snapshot / "state.bin")
        require_file(nvx_snapshot / "mem.bin")

        specs[("nvx", "cold")] = CommandSpec(
            "nvx",
            "cold",
            executable,
            (
                "--kernel",
                str(kernel),
                "--initrd",
                str(initrd),
                "--mem",
                "512",
                "--cmdline",
                NVX_CMDLINE,
                "--mount",
                str(nvx_mount),
                "--exit-on-boot",
                "--quiet",
                "--boot-marker",
                "hello world",
            ),
            NVX_DIR,
            "to marker",
        )
        specs[("nvx", "restore")] = CommandSpec(
            "nvx",
            "restore",
            executable,
            (
                "--restore",
                str(nvx_snapshot),
                "--mem",
                "512",
                "--mount",
                str(nvx_mount),
                "--exit-on-boot",
                "--quiet",
                "--boot-marker",
                "hello world",
            ),
            NVX_DIR,
            "to marker",
        )

    if "nanvix" in selected:
        nanvix_work = artifacts / "nanvix"
        extract_root = nanvix_work / "bundle"
        mount = nanvix_work / "mnt"
        extract_root.mkdir(parents=True, exist_ok=True)
        mount.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample, mount / "hello.py")
        (mount / "argv.txt").write_text("/mnt/hello.py", encoding="ascii")

        release_zip = nanvix_release_zip()
        require_build_receipt(
            "nanvix",
            {"build_patch_sha256": sha256(NANVIX_BUILD_PATCH)},
            {"release_zip": release_zip},
        )
        bundle = extract_root / "nanvix-python-windows-x86-microvm-standalone-256mb"
        if not bundle.is_dir():
            with zipfile.ZipFile(release_zip) as archive:
                archive.extractall(extract_root)
        executable = require_file(bundle / "bin" / "nanvixd.exe")
        bin_dir = bundle / "bin"
        ramfs = require_file(bundle / "nanvix_rootfs.img")
        initrd = require_file(bundle / "python3.initrd")
        snapshot = nanvix_work / "snapshots" / "kernel.whp.cbor"
        nanvix_snapshot_config_path = snapshot.parent / "benchmark-config.json"
        expected_nanvix_snapshot_config: dict[str, object] = {
            "format": 1,
            "executable_sha256": sha256(executable),
            "ramfs_sha256": sha256(ramfs),
            "initrd_sha256": sha256(initrd),
            "build_patch_sha256": sha256(NANVIX_BUILD_PATCH),
            "warmup": GENERIC_SNAPSHOT_WARMUP,
            "workload_in_snapshot": False,
        }
        nanvix_snapshot_config: object = None
        if nanvix_snapshot_config_path.is_file():
            nanvix_snapshot_config = json.loads(
                nanvix_snapshot_config_path.read_text(encoding="utf-8")
            )
        nanvix_snapshot_matches_image = (
            snapshot.is_file()
            and snapshot.with_name("kernel.vmem").is_file()
            and nanvix_snapshot_config == expected_nanvix_snapshot_config
        )
        if not nanvix_snapshot_matches_image:
            if (output_dir / "raw.csv").is_file():
                raise RuntimeError(
                    "Nanvix snapshot does not match the current generic CPython image "
                    f"in {output_dir}; choose a new --output directory"
                )
            shutil.rmtree(snapshot.parent, ignore_errors=True)
            run_control(
                [
                    executable,
                    "-bin-dir",
                    bin_dir,
                    "-ramfs",
                    ramfs,
                    "-kernel-args",
                    "snapshot",
                    "--",
                    initrd,
                ],
                cwd=nanvix_work,
                timeout=timeout,
            )
            nanvix_snapshot_config_path.write_text(
                json.dumps(expected_nanvix_snapshot_config, indent=2),
                encoding="utf-8",
            )
        require_file(snapshot)
        require_file(snapshot.with_name("kernel.vmem"))

        specs[("nanvix", "cold")] = CommandSpec(
            "nanvix",
            "cold",
            executable,
            (
                "-bin-dir",
                str(bin_dir),
                "-ramfs",
                str(ramfs),
                "-mount",
                str(mount),
                "--",
                str(initrd),
            ),
            nanvix_work,
            "hello world",
        )
        specs[("nanvix", "restore")] = CommandSpec(
            "nanvix",
            "restore",
            executable,
            (
                "-bin-dir",
                str(bin_dir),
                "-snapshot",
                str(snapshot),
                "-ramfs",
                str(ramfs),
                "-mount",
                str(mount),
                "-kernel-args",
                "snapshot",
                "--",
                str(initrd),
            ),
            nanvix_work,
            "hello world",
        )

    if "hyperlight" in selected:
        hyperlight_work = artifacts / "hyperlight"
        snapshot = hyperlight_work / "snapshot"
        hyperlight_work.mkdir(parents=True, exist_ok=True)
        runner = require_file(
            HYPERLIGHT_RUNNER_DIR / "target" / "release" / "vmm-hyperlight-runner.exe"
        )
        kernel = require_file(HYPERLIGHT_DRIVER_KERNEL)
        initrd = require_file(HYPERLIGHT_DRIVER_INITRD)
        require_build_receipt(
            "hyperlight",
            {
                "kernel_image": HYPERLIGHT_DRIVER_KERNEL_IMAGE,
                "initrd_image": HYPERLIGHT_DRIVER_INITRD_IMAGE,
            },
            {
                "kernel": kernel,
                "initrd": initrd,
                "runner": runner,
            },
        )
        snapshot_config_path = snapshot / "benchmark-config.json"
        expected_snapshot_config: dict[str, object] = {
            "format": HYPERLIGHT_SNAPSHOT_FORMAT,
            "kernel_sha256": sha256(kernel),
            "initrd_sha256": sha256(initrd),
            "driver_image_tag": HYPERLIGHT_DRIVER_TAG,
            "warmup": GENERIC_SNAPSHOT_WARMUP,
            "workload_in_snapshot": False,
        }
        snapshot_config: object = None
        if snapshot_config_path.is_file():
            snapshot_config = json.loads(snapshot_config_path.read_text(encoding="utf-8"))
        snapshot_matches_image = (
            (snapshot / "index.json").is_file()
            and snapshot_config == expected_snapshot_config
        )
        if not snapshot_matches_image:
            if (output_dir / "raw.csv").is_file():
                raise RuntimeError(
                    "Hyperlight snapshot does not match the current generic CPython "
                    f"image in {output_dir}; choose a new --output directory"
                )
            shutil.rmtree(snapshot, ignore_errors=True)
            run_control(
                [runner, "capture", kernel, initrd, snapshot],
                cwd=HYPERLIGHT_RUNNER_DIR,
                timeout=timeout,
                marker="SNAPSHOT_OK",
            )
            snapshot_config_path.write_text(
                json.dumps(expected_snapshot_config, indent=2),
                encoding="utf-8",
            )
        require_file(snapshot / "index.json")

        specs[("hyperlight", "cold")] = CommandSpec(
            "hyperlight",
            "cold",
            runner,
            ("cold", str(kernel), str(initrd), str(sample)),
            HYPERLIGHT_RUNNER_DIR,
            "BENCHMARK_OK",
        )
        specs[("hyperlight", "restore")] = CommandSpec(
            "hyperlight",
            "restore",
            runner,
            ("restore", str(initrd), str(snapshot), str(sample)),
            HYPERLIGHT_RUNNER_DIR,
            "BENCHMARK_OK",
        )

    manifest_path = output_dir / "manifest.json"
    previous_manifest: dict[str, object] = {}
    if manifest_path.is_file():
        loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded_manifest, dict):
            previous_manifest = loaded_manifest
    previous_commands = previous_manifest.get("commands", {})
    commands = (
        {
            key: value
            for key, value in previous_commands.items()
            if isinstance(key, str) and key.partition("/")[0] in VMM_ORDER
        }
        if isinstance(previous_commands, dict)
        else {}
    )
    commands.update(
        {
            f"{vmm}/{mode}": {
                "command": spec.command,
                "cwd": str(spec.cwd),
                "success_marker": spec.success_marker,
            }
            for (vmm, mode), spec in specs.items()
        }
    )
    manifest = {
        "created_at_utc": previous_manifest.get("created_at_utc", utc_now()),
        "updated_at_utc": utc_now(),
        "sample": {
            "source": str(HELLO_SAMPLE.relative_to(SCRIPT_DIR)),
            "sha256": sha256(sample),
            "delivery": {
                "nvx": {"method": "host_mount", "guest_path": "app.py"},
                "nanvix": {
                    "method": "host_mount",
                    "guest_path": "/mnt/hello.py",
                },
                "hyperlight": {"method": "function_call", "function": "run"},
            },
        },
        "snapshot_policy": {
            "generic_warmup": GENERIC_SNAPSHOT_WARMUP,
            "workload_in_snapshot": False,
            "build_patches": {
                "nvx": {
                    "path": str(NVX_BUILD_PATCH.relative_to(SCRIPT_DIR)),
                    "sha256": sha256(NVX_BUILD_PATCH),
                },
                "nanvix": {
                    "path": str(NANVIX_BUILD_PATCH.relative_to(SCRIPT_DIR)),
                    "sha256": sha256(NANVIX_BUILD_PATCH),
                },
            },
            "hyperlight_driver_images": {
                "kernel": HYPERLIGHT_DRIVER_KERNEL_IMAGE,
                "initrd": HYPERLIGHT_DRIVER_INITRD_IMAGE,
            },
            "capture_points": {
                "nvx": "initialized_untrained_cpython_before_host_mount",
                "nanvix": "initialized_cpython_and_ramfs_before_host_mount",
                "hyperlight": "initialized_cpython_driver",
            },
        },
        "guest_memory_mib": GUEST_MEMORY_MIB,
        "workloads": WORKLOAD_LABELS,
        "commands": commands,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return specs


def git_metadata(repository: Path) -> dict[str, object]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
        return completed.stdout.strip()

    status = git("status", "--porcelain")
    return {
        "path": str(repository),
        "branch": git("branch", "--show-current"),
        "commit": git("rev-parse", "HEAD"),
        "worktree_dirty": bool(status),
        "status": status.splitlines(),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_output(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
            text=True,
        )
        return completed.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unavailable"


def write_metadata(output_dir: Path, specs: dict[tuple[str, str], CommandSpec]) -> None:
    artifact_paths = {spec.executable for spec in specs.values()}
    for spec in specs.values():
        for argument in spec.arguments:
            candidate = Path(argument)
            if candidate.is_file():
                artifact_paths.add(candidate)
                if candidate.name.endswith(".whp.cbor"):
                    memory = candidate.with_name(
                        candidate.name.removesuffix(".whp.cbor") + ".vmem"
                    )
                    if memory.is_file():
                        artifact_paths.add(memory)
    source_artifact_paths = {
        NVX_DIR / "target" / "release" / "microvm.exe",
        NVX_DIR / "build" / "vmlinux",
        NVX_DIR / "build" / "initramfs-python.cpio.gz",
        HYPERLIGHT_RUNNER_DIR / "target" / "release" / "vmm-hyperlight-runner.exe",
        HYPERLIGHT_DRIVER_KERNEL,
        HYPERLIGHT_DRIVER_INITRD,
        HELLO_SAMPLE,
    }
    artifact_paths.update(path for path in source_artifact_paths if path.is_file())
    metadata_path = output_dir / "metadata.json"
    previous_metadata: dict[str, object] = {}
    if metadata_path.is_file():
        loaded_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(loaded_metadata, dict):
            previous_metadata = loaded_metadata
    previous_artifacts = previous_metadata.get("artifacts", [])
    supported_source_artifacts = {path.resolve() for path in source_artifact_paths}
    output_artifacts = (output_dir / "artifacts").resolve()

    def is_supported_artifact(path: Path) -> bool:
        if not path.is_file():
            return False
        resolved = path.resolve()
        if resolved in supported_source_artifacts:
            return True
        try:
            relative = resolved.relative_to(output_artifacts)
        except ValueError:
            return False
        return bool(relative.parts) and relative.parts[0] in VMM_ORDER

    artifacts_by_path = {
        str(record["path"]): record
        for record in previous_artifacts
        if (
            isinstance(record, dict)
            and isinstance(record.get("path"), str)
            and is_supported_artifact(Path(record["path"]))
        )
    }
    for path in sorted(artifact_paths, key=str):
        artifacts_by_path[str(path)] = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        }

    metadata = {
        "captured_at_utc": utc_now(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version,
            "logical_cpu_count": os.cpu_count(),
        },
        "tools": {
            "cargo": version_output(["cargo", "--version"]),
            "rustc": version_output(["rustc", "--version"]),
            "docker": version_output(["docker", "--version"]),
        },
        "repositories": {
            "nvx": git_metadata(NVX_DIR),
            "nanvix-python": git_metadata(NANVIX_DIR),
            "hyperlight-unikraft": git_metadata(HYPERLIGHT_DIR),
        },
        "artifacts": [
            artifacts_by_path[path] for path in sorted(artifacts_by_path)
        ],
        "workloads": WORKLOAD_LABELS,
        "measurement": {
            "elapsed": "perf_counter_ns around direct VMM process creation through exit",
            "peak_rss": "Windows GetProcessMemoryInfo PeakWorkingSetSize after process exit",
            "hyperlight_surrogates": "disabled with configure_surrogates(Some(0))",
            "hyperlight_workload": "host script source passed to initialized CPython function run",
            "hyperlight_phases": "runner-reported persisted snapshot load and first guest call",
            "execution": "sequential, randomized fixed-seed order, one unrecorded preflight per target/mode",
            "cache_policy": "host filesystem caches are not dropped",
        },
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )


RAW_FIELDS = (
    "sequence",
    "timestamp_utc",
    "vmm",
    "mode",
    "sample",
    "elapsed_ms",
    "peak_rss_bytes",
    "peak_rss_mib",
    "exit_code",
    *PHASE_FIELDS,
)


def load_completed(raw_path: Path) -> set[tuple[str, str, int]]:
    if not raw_path.is_file():
        return set()
    with raw_path.open(newline="", encoding="utf-8") as source:
        return {
            (row["vmm"], row["mode"], int(row["sample"]))
            for row in csv.DictReader(source)
        }


def validate_measurement(spec: CommandSpec, result: Measurement) -> None:
    if result.exit_code != 0 or spec.success_marker not in result.output:
        raise RuntimeError(
            f"{spec.vmm}/{spec.mode} failed: exit={result.exit_code}, "
            f"missing_marker={spec.success_marker not in result.output}\n{result.output}"
        )


def run_samples(
    output_dir: Path,
    specs: dict[tuple[str, str], CommandSpec],
    *,
    samples: int,
    seed: int,
    timeout: float,
    cooldown_ms: int,
    preflight: bool,
) -> None:
    raw_path = output_dir / "raw.csv"
    failures = output_dir / "failures"
    failures.mkdir(parents=True, exist_ok=True)
    completed = load_completed(raw_path)
    if raw_path.is_file():
        with raw_path.open(newline="", encoding="utf-8") as source:
            existing_fields = tuple(csv.DictReader(source).fieldnames or ())
        if existing_fields != RAW_FIELDS:
            raise RuntimeError(
                f"raw.csv in {output_dir} uses an incompatible measurement schema; "
                "choose a new --output directory"
            )

    if preflight:
        completed_groups = {(vmm, mode) for vmm, mode, _ in completed}
        preflight_keys = [key for key in specs if key not in completed_groups]
        if preflight_keys:
            print("Running one unrecorded preflight per new target/mode...", flush=True)
        for key in sorted(
            preflight_keys,
            key=lambda item: (VMM_ORDER.index(item[0]), MODE_ORDER.index(item[1])),
        ):
            spec = specs[key]
            result = measure(spec, timeout)
            validate_measurement(spec, result)

    jobs = [
        (vmm, mode, sample)
        for (vmm, mode) in specs
        for sample in range(1, samples + 1)
        if (vmm, mode, sample) not in completed
    ]
    random.Random(seed).shuffle(jobs)

    write_header = not raw_path.is_file()
    sequence = len(completed)
    with raw_path.open("a", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=RAW_FIELDS)
        if write_header:
            writer.writeheader()
            destination.flush()
            os.fsync(destination.fileno())

        total = len(jobs)
        for index, (vmm, mode, sample) in enumerate(jobs, start=1):
            spec = specs[(vmm, mode)]
            try:
                result = measure(spec, timeout)
                validate_measurement(spec, result)
            except Exception as error:
                failure_path = failures / f"{vmm}-{mode}-{sample:03d}.log"
                failure_path.write_text(str(error), encoding="utf-8")
                raise RuntimeError(f"sample failed; details: {failure_path}") from error

            sequence += 1
            row = {
                "sequence": sequence,
                "timestamp_utc": utc_now(),
                "vmm": vmm,
                "mode": mode,
                "sample": sample,
                "elapsed_ms": f"{result.elapsed_ms:.6f}",
                "peak_rss_bytes": result.peak_rss_bytes,
                "peak_rss_mib": f"{result.peak_rss_bytes / (1024 * 1024):.6f}",
                "exit_code": result.exit_code,
                **{
                    field: (
                        f"{result.phases_ms[field]:.6f}"
                        if field in result.phases_ms
                        else ""
                    )
                    for field in PHASE_FIELDS
                },
            }
            writer.writerow(row)
            destination.flush()
            os.fsync(destination.fileno())
            print(
                f"[{index:03d}/{total:03d}] {vmm:10s} {mode:7s} "
                f"sample={sample:03d} time={result.elapsed_ms:9.3f} ms "
                f"peak-rss={result.peak_rss_bytes / (1024 * 1024):8.2f} MiB",
                flush=True,
            )
            if cooldown_ms:
                time.sleep(cooldown_ms / 1000.0)


def percentile(values: Sequence[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile of an empty sequence")
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def describe(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


def load_groups(raw_path: Path) -> dict[tuple[str, str], dict[str, list[float]]]:
    groups: dict[tuple[str, str], dict[str, list[float]]] = {}
    with raw_path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            vmm = row["vmm"]
            mode = row["mode"]
            if vmm not in VMM_ORDER or mode not in MODE_ORDER:
                continue
            key = (vmm, mode)
            group = groups.setdefault(
                key,
                {
                    "elapsed_ms": [],
                    "peak_rss_mib": [],
                    **{field: [] for field in PHASE_FIELDS},
                },
            )
            group["elapsed_ms"].append(float(row["elapsed_ms"]))
            group["peak_rss_mib"].append(float(row["peak_rss_mib"]))
            for field in PHASE_FIELDS:
                value = row.get(field, "")
                if value:
                    group[field].append(float(value))
    return groups


def write_cdf_svg(
    path: Path,
    groups: dict[tuple[str, str], dict[str, list[float]]],
    *,
    vmms: Sequence[str],
    metric: str,
    mode: str,
    unit: str,
    title: str,
    log2_x: bool = False,
    interpolate: bool = False,
    zoom_insets: bool = False,
) -> None:
    values_by_vmm = {
        vmm: groups[(vmm, mode)][metric]
        for vmm in vmms
        if (vmm, mode) in groups
    }
    if not values_by_vmm:
        raise ValueError(f"no {mode} values available for {metric}")
    values_by_vmm = dict(
        sorted(
            values_by_vmm.items(),
            key=lambda item: statistics.median(item[1]),
        )
    )

    all_values = [value for values in values_by_vmm.values() for value in values]
    observed_minimum = min(all_values)
    observed_maximum = max(all_values)
    if log2_x:
        if observed_minimum <= 0:
            raise ValueError("base-2 logarithmic CDF axes require positive values")
        minimum_exponent = math.floor(math.log2(observed_minimum))
        maximum_exponent = math.ceil(math.log2(observed_maximum))
        if minimum_exponent == maximum_exponent:
            maximum_exponent += 1
        minimum = 2.0**minimum_exponent
        maximum = 2.0**maximum_exponent
        x_ticks = [
            2.0**exponent
            for exponent in range(minimum_exponent, maximum_exponent + 1)
        ]
    else:
        span = observed_maximum - observed_minimum
        if math.isclose(span, 0.0):
            span = max(abs(observed_minimum) * 0.02, 1.0)
        minimum = 0.0
        maximum = observed_maximum + span * 0.03
        x_ticks = [minimum + tick * (maximum - minimum) / 4 for tick in range(5)]

    width = 920 if len(values_by_vmm) <= 3 else 1480
    height = 790 if zoom_insets else 570
    plot_left = 95
    plot_top = 105
    plot_width = width - plot_left - 55
    plot_height = 285 if zoom_insets else 390
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#111827}"
        ".title{font-size:22px;font-weight:600}.legend{font-size:12px;font-weight:600}"
        ".tick{font-size:12px}.label{font-size:13px}.axis{stroke:#374151;stroke-width:1}"
        ".grid{stroke:#d1d5db;stroke-width:1}.cdf{fill:none;stroke-width:2.5;"
        "stroke-linejoin:round}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="34" text-anchor="middle" class="title">'
        f"{html.escape(title)}</text>",
    ]

    def x_coordinate(value: float) -> float:
        if log2_x:
            scaled_value = math.log2(value)
            scaled_minimum = math.log2(minimum)
            scaled_maximum = math.log2(maximum)
        else:
            scaled_value = value
            scaled_minimum = minimum
            scaled_maximum = maximum
        return (
            plot_left
            + (scaled_value - scaled_minimum)
            * plot_width
            / (scaled_maximum - scaled_minimum)
        )

    def y_coordinate(probability: float) -> float:
        return plot_top + plot_height * (1.0 - probability)

    for tick in range(5):
        probability = tick / 4
        y = y_coordinate(probability)
        elements.append(
            f'<line x1="{plot_left}" y1="{y:.1f}" x2="{plot_left + plot_width}" '
            f'y2="{y:.1f}" class="grid"/>'
        )
        elements.append(
            f'<text x="{plot_left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="tick">{probability:.0%}</text>'
        )

    for value in x_ticks:
        x = x_coordinate(value)
        elements.append(
            f'<line x1="{x:.1f}" y1="{plot_top}" x2="{x:.1f}" '
            f'y2="{plot_top + plot_height}" class="grid"/>'
        )
        elements.append(
            f'<text x="{x:.1f}" y="{plot_top + plot_height + 22}" '
            f'text-anchor="middle" class="tick">'
            f"{value:g}</text>"
        )

    for index, (vmm, values) in enumerate(values_by_vmm.items()):
        legend_x = plot_left + index * plot_width / len(values_by_vmm)
        elements.extend(
            [
                f'<line x1="{legend_x}" y1="72" x2="{legend_x + 22}" y2="72" '
                f'stroke="{VMM_PLOT_COLORS[vmm]}" stroke-width="3"/>',
                f'<text x="{legend_x + 30}" y="77" class="legend">'
                f"{html.escape(VMM_PLOT_LABELS[vmm])}</text>",
            ]
        )
        ordered = sorted(values)
        unique_values: list[tuple[float, int]] = []
        for value in ordered:
            if unique_values and value == unique_values[-1][0]:
                previous_value, count = unique_values[-1]
                unique_values[-1] = (previous_value, count + 1)
            else:
                unique_values.append((value, 1))

        path_parts = [f"M {x_coordinate(minimum):.2f} {y_coordinate(0.0):.2f}"]
        cumulative = 0
        for value, count in unique_values:
            cumulative += count
            x = x_coordinate(value)
            y = y_coordinate(cumulative / len(ordered))
            if interpolate and cumulative != count:
                path_parts.append(f"L {x:.2f} {y:.2f}")
            else:
                path_parts.append(f"H {x:.2f}")
                path_parts.append(f"V {y:.2f}")
        path_parts.append(f"H {x_coordinate(maximum):.2f}")
        elements.append(
            f'<path d="{" ".join(path_parts)}" class="cdf" '
            f'stroke="{VMM_PLOT_COLORS[vmm]}"/>'
        )

    elements.extend(
        [
            f'<line x1="{plot_left}" y1="{plot_top + plot_height}" '
            f'x2="{plot_left + plot_width}" y2="{plot_top + plot_height}" class="axis"/>',
            f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" '
            f'y2="{plot_top + plot_height}" class="axis"/>',
            f'<text x="{plot_left + plot_width / 2}" '
            f'y="{plot_top + plot_height + 55}" '
            f'text-anchor="middle" class="label">{html.escape(unit)}</text>',
            f'<text x="24" y="{plot_top + plot_height / 2}" text-anchor="middle" '
            f'transform="rotate(-90 24 {plot_top + plot_height / 2})" '
            f'class="label">Cumulative Probability</text>',
        ]
    )

    if zoom_insets:
        elements.append(
            '<text x="36" y="485" class="legend">'
            "Per-VMM zooms (local log2 x-ranges)</text>"
        )
        inset_top = 525
        inset_height = 170
        inset_width = 210
        for index, (vmm, values) in enumerate(values_by_vmm.items()):
            inset_left = 75 + index * 295
            ordered = sorted(values)
            local_observed_minimum = ordered[0]
            local_observed_maximum = ordered[-1]
            local_log_minimum = math.log2(local_observed_minimum)
            local_log_maximum = math.log2(local_observed_maximum)
            local_span = local_log_maximum - local_log_minimum
            if math.isclose(local_span, 0.0):
                local_span = 0.001
            local_log_minimum -= local_span * 0.08
            local_log_maximum += local_span * 0.08
            local_minimum = 2.0**local_log_minimum
            local_maximum = 2.0**local_log_maximum

            def inset_x_coordinate(value: float) -> float:
                return (
                    inset_left
                    + (math.log2(value) - local_log_minimum)
                    * inset_width
                    / (local_log_maximum - local_log_minimum)
                )

            def inset_y_coordinate(probability: float) -> float:
                return inset_top + inset_height * (1.0 - probability)

            elements.append(
                f'<text x="{inset_left + inset_width / 2}" y="{inset_top - 12}" '
                f'text-anchor="middle" class="legend" fill="{VMM_PLOT_COLORS[vmm]}">'
                f"{html.escape(vmm)}</text>"
            )
            for probability in (0.0, 0.5, 1.0):
                y = inset_y_coordinate(probability)
                elements.extend(
                    [
                        f'<line x1="{inset_left}" y1="{y:.1f}" '
                        f'x2="{inset_left + inset_width}" y2="{y:.1f}" class="grid"/>',
                        f'<text x="{inset_left - 6}" y="{y + 4:.1f}" '
                        f'text-anchor="end" class="tick">{probability:.0%}</text>',
                    ]
                )
            local_ticks = [
                local_minimum,
                2.0 ** ((local_log_minimum + local_log_maximum) / 2),
                local_maximum,
            ]
            for value in local_ticks:
                x = inset_x_coordinate(value)
                elements.extend(
                    [
                        f'<line x1="{x:.1f}" y1="{inset_top}" x2="{x:.1f}" '
                        f'y2="{inset_top + inset_height}" class="grid"/>',
                        f'<text x="{x:.1f}" y="{inset_top + inset_height + 18}" '
                        f'text-anchor="middle" class="tick">{value:.2f}</text>',
                    ]
                )

            unique_values: list[tuple[float, int]] = []
            for value in ordered:
                if unique_values and value == unique_values[-1][0]:
                    previous_value, count = unique_values[-1]
                    unique_values[-1] = (previous_value, count + 1)
                else:
                    unique_values.append((value, 1))
            inset_path = [
                f"M {inset_x_coordinate(local_minimum):.2f} "
                f"{inset_y_coordinate(0.0):.2f}"
            ]
            cumulative = 0
            for value, count in unique_values:
                cumulative += count
                x = inset_x_coordinate(value)
                y = inset_y_coordinate(cumulative / len(ordered))
                if cumulative == count:
                    inset_path.extend([f"H {x:.2f}", f"V {y:.2f}"])
                else:
                    inset_path.append(f"L {x:.2f} {y:.2f}")
            inset_path.append(f"H {inset_x_coordinate(local_maximum):.2f}")
            elements.extend(
                [
                    f'<path d="{" ".join(inset_path)}" class="cdf" '
                    f'stroke="{VMM_PLOT_COLORS[vmm]}" stroke-width="2"/>',
                    f'<line x1="{inset_left}" y1="{inset_top + inset_height}" '
                    f'x2="{inset_left + inset_width}" y2="{inset_top + inset_height}" '
                    f'class="axis"/>',
                    f'<line x1="{inset_left}" y1="{inset_top}" x2="{inset_left}" '
                    f'y2="{inset_top + inset_height}" class="axis"/>',
                    f'<text x="{inset_left + inset_width / 2}" y="758" '
                    f'text-anchor="middle" class="tick">MiB (local log2)</text>',
                ]
            )

    elements.append("</svg>")
    path.write_text("\n".join(elements), encoding="utf-8")


def write_rss_barplot_svg(
    path: Path,
    groups: dict[tuple[str, str], dict[str, list[float]]],
    *,
    vmms: Sequence[str],
    mode: str,
    title: str,
) -> None:
    values_by_vmm = {
        vmm: groups[(vmm, mode)]["peak_rss_mib"]
        for vmm in vmms
        if (vmm, mode) in groups
    }
    if not values_by_vmm:
        raise ValueError(f"no {mode} peak-RSS values available")
    if any(value < 0 for values in values_by_vmm.values() for value in values):
        raise ValueError("RSS plots require non-negative values")
    p99_by_vmm = {
        vmm: percentile(values, 99) for vmm, values in values_by_vmm.items()
    }
    displayed_maximum = max(p99_by_vmm.values())
    if displayed_maximum == 0:
        raise ValueError("at least one displayed RSS value must be positive")
    padded_maximum = displayed_maximum * 1.05
    maximum_magnitude = 10.0 ** math.floor(math.log10(padded_maximum))
    axis_maximum = next(
        factor * maximum_magnitude
        for factor in (1.0, 2.0, 2.5, 5.0, 10.0)
        if factor * maximum_magnitude >= padded_maximum
    )

    width = 920
    height = 620
    plot_left = 95
    plot_top = 90
    plot_width = width - plot_left - 55
    plot_height = 390
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#111827}"
        ".title{font-size:22px;font-weight:600}.value{font-size:11px;font-weight:600}"
        ".tick{font-size:11px}"
        ".label{font-size:13px}.axis{stroke:#374151;stroke-width:1}"
        ".grid{stroke:#d1d5db;stroke-width:1}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="34" text-anchor="middle" class="title">'
        f"{html.escape(title)}</text>",
    ]

    def y_coordinate(value: float) -> float:
        return plot_top + (axis_maximum - value) * plot_height / axis_maximum

    for tick in range(5):
        value = tick * axis_maximum / 4
        y = y_coordinate(value)
        elements.extend(
            [
                f'<line x1="{plot_left}" y1="{y:.1f}" '
                f'x2="{plot_left + plot_width}" y2="{y:.1f}" class="grid"/>',
                f'<text x="{plot_left - 7}" y="{y + 4:.1f}" '
                f'text-anchor="end" class="tick">{value:g}</text>',
            ]
        )

    slot_width = plot_width / len(values_by_vmm)
    baseline_y = y_coordinate(0)
    for index, (vmm, p99) in enumerate(p99_by_vmm.items()):
        center = plot_left + (index + 0.5) * slot_width
        bar_width = min(92.0, slot_width * 0.42)

        p99_y = y_coordinate(p99)
        elements.extend(
            [
                f'<rect x="{center - bar_width / 2}" y="{p99_y:.1f}" '
                f'width="{bar_width}" height="{max(baseline_y - p99_y, 1.0):.1f}" '
                f'fill="{VMM_PLOT_COLORS[vmm]}" '
                f'stroke="{VMM_PLOT_COLORS[vmm]}" '
                f'stroke-width="2" class="rss-bar"/>',
                f'<text x="{center}" y="{max(p99_y - 10, plot_top + 14):.1f}" '
                f'text-anchor="middle" class="value">{p99:.2f} MiB</text>',
            ]
        )
        elements.append(
            f'<text x="{center}" y="{baseline_y + 26:.1f}" text-anchor="middle" '
            f'class="tick">{html.escape(VMM_PLOT_LABELS[vmm])}</text>'
        )

    elements.extend(
        [
            f'<line x1="{plot_left}" y1="{baseline_y}" '
            f'x2="{plot_left + plot_width}" y2="{baseline_y}" class="axis"/>',
            f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" '
            f'y2="{baseline_y}" class="axis"/>',
            f'<text x="24" y="{plot_top + plot_height / 2}" text-anchor="middle" '
            f'transform="rotate(-90 24 {plot_top + plot_height / 2})" '
            f'class="label">P99 Peak Resident Memory (MiB)</text>',
            f'<text x="{width / 2}" y="540" text-anchor="middle" '
            f'class="label">VMM</text>',
            f'<text x="{width / 2}" y="580" text-anchor="middle" class="tick">'
            f"Bars: P99 peak resident memory</text>",
        ]
    )
    elements.append("</svg>")
    path.write_text("\n".join(elements), encoding="utf-8")


def find_edge() -> Path:
    candidates = [
        shutil.which("msedge"),
        str(
            Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"))
            / "Microsoft"
            / "Edge"
            / "Application"
            / "msedge.exe"
        ),
        str(
            Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
            / "Microsoft"
            / "Edge"
            / "Application"
            / "msedge.exe"
        ),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError("Microsoft Edge is required to rasterize plot SVGs as PNGs")


def render_svg_as_png(svg_path: Path) -> Path:
    root = ElementTree.parse(svg_path).getroot()
    width = int(float(root.attrib["width"]))
    height = int(float(root.attrib["height"]))
    png_path = svg_path.with_suffix(".png")
    png_path.unlink(missing_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="vmm-benchmark-edge-"))
    command = [
        str(find_edge()),
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--force-device-scale-factor=1",
        f"--window-size={width},{height}",
        f"--user-data-dir={profile}",
        f"--screenshot={png_path}",
        svg_path.resolve().as_uri(),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def png_is_complete() -> bool:
        if not png_path.is_file() or png_path.stat().st_size < 45:
            return False
        with png_path.open("rb") as source:
            header = source.read(24)
            source.seek(-12, os.SEEK_END)
            trailer = source.read(12)
        return (
            header[:8] == b"\x89PNG\r\n\x1a\n"
            and header[12:16] == b"IHDR"
            and int.from_bytes(header[16:20], "big") == width
            and int.from_bytes(header[20:24], "big") == height
            and trailer[4:8] == b"IEND"
        )

    try:
        deadline = time.monotonic() + 30
        while not png_is_complete():
            returncode = process.poll()
            if returncode is not None:
                raise RuntimeError(
                    f"failed to render {svg_path.name} as PNG (exit {returncode})"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out rendering {svg_path.name} as PNG")
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
    return png_path


def analyze(output_dir: Path, expected_samples: int | None = None) -> list[dict[str, object]]:
    raw_path = require_file(output_dir / "raw.csv")
    groups = load_groups(raw_path)
    if not groups:
        raise RuntimeError(f"no benchmark records in {raw_path}")
    current_plot_stems = {
        f"{kind}_{workload}_{mode}"
        for kind in ("cdf_execution_time", "barplot_peak_rss")
        for workload in PLOT_WORKLOAD_ORDER
        for mode in MODE_ORDER
    }
    for pattern in ("cdf_execution_time_*.*", "barplot_peak_rss_*.*"):
        for plot in output_dir.glob(pattern):
            if plot.stem not in current_plot_stems:
                plot.unlink()
    summaries: list[dict[str, object]] = []
    phase_summaries: list[dict[str, object]] = []
    for vmm in VMM_ORDER:
        for mode in MODE_ORDER:
            key = (vmm, mode)
            if key not in groups:
                continue
            elapsed = describe(groups[key]["elapsed_ms"])
            rss = describe(groups[key]["peak_rss_mib"])
            if expected_samples is not None and elapsed["count"] != expected_samples:
                raise RuntimeError(
                    f"{vmm}/{mode} has {elapsed['count']} samples; expected {expected_samples}"
                )
            summaries.append(
                {
                    "vmm": vmm,
                    "workload": WORKLOAD_LABELS[vmm],
                    "mode": mode,
                    "execution_time_ms": elapsed,
                    "peak_rss_mib": rss,
                }
            )
            for phase in PHASE_FIELDS:
                values = groups[key][phase]
                if values:
                    phase_summaries.append(
                        {
                            "vmm": vmm,
                            "mode": mode,
                            "phase": phase,
                            "time_ms": describe(values),
                        }
                    )

    (output_dir / "summary.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8"
    )
    summary_fields = [
        "vmm",
        "workload",
        "mode",
        "count",
        "time_mean_ms",
        "time_stdev_ms",
        "time_min_ms",
        "time_p50_ms",
        "time_p95_ms",
        "time_p99_ms",
        "time_max_ms",
        "rss_mean_mib",
        "rss_stdev_mib",
        "rss_min_mib",
        "rss_p50_mib",
        "rss_p95_mib",
        "rss_p99_mib",
        "rss_max_mib",
    ]
    with (output_dir / "summary.csv").open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=summary_fields)
        writer.writeheader()
        for summary in summaries:
            elapsed = summary["execution_time_ms"]
            rss = summary["peak_rss_mib"]
            assert isinstance(elapsed, dict) and isinstance(rss, dict)
            writer.writerow(
                {
                    "vmm": summary["vmm"],
                    "workload": summary["workload"],
                    "mode": summary["mode"],
                    "count": elapsed["count"],
                    "time_mean_ms": f"{elapsed['mean']:.6f}",
                    "time_stdev_ms": f"{elapsed['stdev']:.6f}",
                    "time_min_ms": f"{elapsed['min']:.6f}",
                    "time_p50_ms": f"{elapsed['p50']:.6f}",
                    "time_p95_ms": f"{elapsed['p95']:.6f}",
                    "time_p99_ms": f"{elapsed['p99']:.6f}",
                    "time_max_ms": f"{elapsed['max']:.6f}",
                    "rss_mean_mib": f"{rss['mean']:.6f}",
                    "rss_stdev_mib": f"{rss['stdev']:.6f}",
                    "rss_min_mib": f"{rss['min']:.6f}",
                    "rss_p50_mib": f"{rss['p50']:.6f}",
                    "rss_p95_mib": f"{rss['p95']:.6f}",
                    "rss_p99_mib": f"{rss['p99']:.6f}",
                    "rss_max_mib": f"{rss['max']:.6f}",
                }
            )

    (output_dir / "phase_summary.json").write_text(
        json.dumps(phase_summaries, indent=2), encoding="utf-8"
    )
    phase_summary_fields = [
        "vmm",
        "mode",
        "phase",
        "count",
        "time_mean_ms",
        "time_stdev_ms",
        "time_min_ms",
        "time_p50_ms",
        "time_p95_ms",
        "time_p99_ms",
        "time_max_ms",
    ]
    with (output_dir / "phase_summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as destination:
        writer = csv.DictWriter(destination, fieldnames=phase_summary_fields)
        writer.writeheader()
        for phase_summary in phase_summaries:
            timing = phase_summary["time_ms"]
            assert isinstance(timing, dict)
            writer.writerow(
                {
                    "vmm": phase_summary["vmm"],
                    "mode": phase_summary["mode"],
                    "phase": phase_summary["phase"],
                    "count": timing["count"],
                    "time_mean_ms": f"{timing['mean']:.6f}",
                    "time_stdev_ms": f"{timing['stdev']:.6f}",
                    "time_min_ms": f"{timing['min']:.6f}",
                    "time_p50_ms": f"{timing['p50']:.6f}",
                    "time_p95_ms": f"{timing['p95']:.6f}",
                    "time_p99_ms": f"{timing['p99']:.6f}",
                    "time_max_ms": f"{timing['max']:.6f}",
                }
            )

    for workload in PLOT_WORKLOAD_ORDER:
        workload_label = PLOT_WORKLOAD_LABELS[workload]
        vmms = PLOT_WORKLOAD_VMMS[workload]
        for mode in MODE_ORDER:
            if not any((vmm, mode) in groups for vmm in vmms):
                continue
            execution_plot = (
                output_dir / f"cdf_execution_time_{workload}_{mode}.svg"
            )
            write_cdf_svg(
                execution_plot,
                groups,
                vmms=vmms,
                metric="elapsed_ms",
                mode=mode,
                unit="Execution Time (ms)",
                title=(
                    f"{workload_label} Hello-World {MODE_LABELS[mode]} "
                    "Execution Time CDF"
                ),
            )
            render_svg_as_png(execution_plot)
            rss_plot = output_dir / f"barplot_peak_rss_{workload}_{mode}.svg"
            write_rss_barplot_svg(
                rss_plot,
                groups,
                vmms=tuple(vmm for vmm in RSS_VMM_ORDER if vmm in vmms),
                mode=mode,
                title=(
                    f"{workload_label} Hello-World {MODE_LABELS[mode]} "
                    "P99 Peak Resident Memory"
                ),
            )
            render_svg_as_png(rss_plot)

    for mode in MODE_ORDER:
        for stem in (
            f"cdf_execution_time_{mode}",
            f"barplot_peak_rss_{mode}",
            f"boxplot_peak_rss_{mode}",
        ):
            for extension in ("svg", "png"):
                legacy_plot = output_dir / f"{stem}.{extension}"
                if legacy_plot.is_file():
                    legacy_plot.unlink()
    write_report(output_dir, summaries, phase_summaries)
    return summaries


def write_report(
    output_dir: Path,
    summaries: Sequence[dict[str, object]],
    phase_summaries: Sequence[dict[str, object]],
) -> None:
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    memory = manifest["guest_memory_mib"]
    included_vmms = {
        str(summary["vmm"])
        for summary in summaries
    }
    snapshot_policy_value = manifest.get("snapshot_policy")
    snapshot_policy = (
        cast(dict[str, object], snapshot_policy_value)
        if isinstance(snapshot_policy_value, dict)
        else None
    )
    generic_snapshot_policy = False
    if snapshot_policy is not None:
        generic_snapshot_policy = (
            snapshot_policy.get("generic_warmup") == GENERIC_SNAPSHOT_WARMUP
            and snapshot_policy.get("workload_in_snapshot") is False
        )
    if generic_snapshot_policy:
        snapshot_method_lines = [
            "- Every snapshot executes the generic Python statement `pass` before capture.",
        ]
        if "hyperlight" in included_vmms:
            snapshot_method_lines.append(
                "- Hyperlight snapshots an initialized CPython driver, then passes the sample "
                "source to its `run` function after boot or restore; workload source is absent "
                "from the kernel, initrd, and snapshot."
            )
    else:
        snapshot_method_lines = [
            "- This result predates explicit snapshot-policy metadata; generic warmup and "
            "workload-independent capture cannot be inferred from these files."
        ]
    memory_text = ", ".join(
        f"{vmm} uses {memory[vmm]} MiB"
        for vmm in VMM_ORDER
        if vmm in included_vmms and vmm in memory
    )
    summary_by_key = {
        (str(summary["vmm"]), str(summary["mode"])): summary for summary in summaries
    }
    lines = [
        "# VMM hello-world benchmark",
        "",
        "End-to-end process lifecycle and runner-internal phases are reported separately. "
        "Every end-to-end sample starts a new host process. Host filesystem caches were "
        "warmed by one unrecorded preflight and were not dropped.",
        "",
        "## End-to-end process lifecycle",
    ]
    cold_description = (
        "A fresh process constructs a sandbox from kernel/initrd and runs the workload; "
        "no persisted VM snapshot is loaded."
    )
    if "hyperlight" in included_vmms:
        cold_description += (
            " Hyperlight's build/evolve API still captures and rewinds an in-memory "
            "post-evolve snapshot before its first call."
        )
    mode_descriptions = {
        "cold": cold_description,
        "restore": (
            "A fresh process loads one reusable persisted snapshot, invokes the workload, "
            "and exits."
        ),
    }
    for mode in MODE_ORDER:
        available = [
            summary
            for summary in summaries
            if str(summary["mode"]) == mode
        ]
        if not available:
            continue
        lines.extend(
            [
                "",
                f"### {MODE_LABELS[mode]}",
                "",
                mode_descriptions[mode],
                "",
                "| Target | Workload | n | Guest MiB | Median time (ms) | p95 time (ms) | "
                "Median peak RSS (MiB) | p95 peak RSS (MiB) | Max peak RSS (MiB) |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for vmm in VMM_ORDER:
            summary = summary_by_key.get((vmm, mode))
            if summary is None:
                continue
            elapsed = summary["execution_time_ms"]
            rss = summary["peak_rss_mib"]
            assert isinstance(elapsed, dict) and isinstance(rss, dict)
            lines.append(
                f"| {VMM_LABELS[vmm]} | {summary['workload']} | {elapsed['count']} | "
                f"{memory[vmm]} | {elapsed['p50']:.3f} | {elapsed['p95']:.3f} | "
                f"{rss['p50']:.2f} | {rss['p95']:.2f} | {rss['max']:.2f} |"
            )
    lines.extend(
        [
            "",
            "## Snapshot effect",
            "",
            "| Target | Workload | Median speedup | Median time reduction | "
            "Median peak-RSS reduction |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for vmm in VMM_ORDER:
        cold = summary_by_key.get((vmm, "cold"))
        restore = summary_by_key.get((vmm, "restore"))
        if cold is None or restore is None:
            continue
        cold_time = cold["execution_time_ms"]
        restore_time = restore["execution_time_ms"]
        cold_rss = cold["peak_rss_mib"]
        restore_rss = restore["peak_rss_mib"]
        assert isinstance(cold_time, dict) and isinstance(restore_time, dict)
        assert isinstance(cold_rss, dict) and isinstance(restore_rss, dict)
        speedup = cold_time["p50"] / restore_time["p50"]
        time_reduction = 100.0 * (1.0 - restore_time["p50"] / cold_time["p50"])
        rss_reduction = 100.0 * (1.0 - restore_rss["p50"] / cold_rss["p50"])
        lines.append(
            f"| {VMM_LABELS[vmm]} | {WORKLOAD_LABELS[vmm]} | {speedup:.2f}x | "
            f"{time_reduction:.1f}% | {rss_reduction:.1f}% |"
        )
    if phase_summaries:
        lines.extend(
            [
                "",
                "## Runner-internal phases",
                "",
                "These timers are emitted inside the Hyperlight runner and are not substitutes "
                "for end-to-end latency. Phase percentiles are computed independently, so their "
                "medians do not necessarily sum to the end-to-end median.",
            ]
        )
        for mode in MODE_ORDER:
            mode_phases = [
                summary
                for summary in phase_summaries
                if str(summary["mode"]) == mode
            ]
            if not mode_phases:
                continue
            lines.extend(
                [
                    "",
                    f"### {MODE_LABELS[mode]} phases",
                    "",
                    "| Target | Phase | n | Median (ms) | p95 (ms) |",
                    "|---|---|---:|---:|---:|",
                ]
            )
            for phase_summary in mode_phases:
                timing = phase_summary["time_ms"]
                assert isinstance(timing, dict)
                vmm = str(phase_summary["vmm"])
                phase = str(phase_summary["phase"])
                lines.append(
                    f"| {VMM_LABELS[vmm]} | {PHASE_LABELS[phase]} | "
                    f"{timing['count']} | {timing['p50']:.3f} | {timing['p95']:.3f} |"
                )
        lines.extend(
            [
                "",
                "The first guest invocation includes demand paging plus workload execution. "
                "On cold Hyperlight runs it also includes CPython initialization. Remaining "
                "process lifecycle is the end-to-end duration minus emitted internal phases and "
                "covers process startup, argument and script handling, output, and teardown.",
                "",
                "Steady-state in-memory rewind is not measured by this one-process-per-sample "
                "benchmark and is not inferred from persisted snapshot load time.",
            ]
        )
    for workload in PLOT_WORKLOAD_ORDER:
        vmms = PLOT_WORKLOAD_VMMS[workload]
        available_modes = [
            mode
            for mode in MODE_ORDER
            if any((vmm, mode) in summary_by_key for vmm in vmms)
        ]
        if not available_modes:
            continue
        workload_label = PLOT_WORKLOAD_LABELS[workload]
        lines.extend(["", f"## {workload_label} plots"])
        for mode in available_modes:
            mode_label = MODE_LABELS[mode]
            lines.extend(
                [
                    "",
                    f"### {mode_label}",
                    "",
                    f"![{workload_label} {mode_label} execution-time CDF]"
                    f"(cdf_execution_time_{workload}_{mode}.svg)",
                    "",
                    f"![{workload_label} {mode_label} P99 peak-RSS bar plot]"
                    f"(barplot_peak_rss_{workload}_{mode}.svg)",
                ]
            )
    lines.extend(["", "## Revisions", ""])
    for name, repository in metadata["repositories"].items():
        worktree = " with local changes" if repository["worktree_dirty"] else ""
        lines.append(
            f"- **{name}:** `{repository['branch']}` at `{repository['commit']}`"
            f"{worktree}"
        )
    lines.extend(
        [
            "",
            "## Method",
            "",
            "- Wall time uses `perf_counter_ns()` from direct process creation through exit.",
            "- Peak RSS is Windows `GetProcessMemoryInfo().PeakWorkingSetSize` for that process.",
            *(
                ["- Hyperlight surrogate processes are disabled, keeping one sandbox in one process."]
                if "hyperlight" in included_vmms
                else []
            ),
            *snapshot_method_lines,
            "- Samples run sequentially in a deterministic randomized order with a 100 ms cooldown.",
            "- CDF legends follow the curves from left to right at the median.",
            "- Plot colors are consistent: NVX is blue, Nanvix is green, "
            "and Hyperlight is red.",
            "- Peak-RSS figures place all VMM bars on one shared plotting area, "
            "using P99 bars and a zero-based linear y-axis sized to the values displayed "
            "in that plot.",
            "- Every plot is saved in both SVG and PNG formats.",
            f"- Guest memory configuration: {memory_text}.",
            "",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def print_summary(summaries: Iterable[dict[str, object]]) -> None:
    print(
        "\nVMM         mode      n  median-ms   p95-ms  median-RSS-MiB  p95-RSS-MiB",
        flush=True,
    )
    for summary in summaries:
        elapsed = summary["execution_time_ms"]
        rss = summary["peak_rss_mib"]
        assert isinstance(elapsed, dict) and isinstance(rss, dict)
        print(
            f"{str(summary['vmm']):11s} {str(summary['mode']):7s} "
            f"{elapsed['count']:3d} {elapsed['p50']:10.3f} {elapsed['p95']:9.3f} "
            f"{rss['p50']:15.2f} {rss['p95']:12.2f}",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build",
        action="store_true",
        help="build all selected targets using their checked-out branch instructions",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="prepare reusable snapshots, then stop before sampling",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="regenerate summaries and plots from an existing raw.csv",
    )
    parser.add_argument("--samples", type=int, default=100, help="samples per target/mode")
    parser.add_argument("--seed", type=int, default=0x5EED, help="sample-order seed")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-process timeout")
    parser.add_argument("--cooldown-ms", type=int, default=100)
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument(
        "--vmm",
        action="append",
        choices=VMM_ORDER,
        help="limit to one or more VMM/workload targets; may be repeated",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="result directory; an existing directory resumes missing samples",
    )
    return parser.parse_args()


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("this benchmark currently requires Windows/WHP")
    args = parse_args()
    if args.samples <= 0:
        raise ValueError("--samples must be positive")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.cooldown_ms < 0:
        raise ValueError("--cooldown-ms cannot be negative")
    selected = set(args.vmm or VMM_ORDER)
    output_dir = (
        args.output.resolve()
        if args.output
        else SCRIPT_DIR
        / "results"
        / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.analyze_only:
        summaries = analyze(output_dir, expected_samples=args.samples)
        print_summary(summaries)
        return 0

    if args.build:
        build_projects(selected)
    specs = prepare(
        output_dir,
        selected,
        args.timeout,
    )
    write_metadata(output_dir, specs)
    if args.prepare_only:
        print(f"Prepared artifacts in {output_dir}")
        return 0

    run_samples(
        output_dir,
        specs,
        samples=args.samples,
        seed=args.seed,
        timeout=args.timeout,
        cooldown_ms=args.cooldown_ms,
        preflight=not args.no_preflight,
    )
    summaries = analyze(output_dir, expected_samples=args.samples)
    print_summary(summaries)
    print(f"\nResults: {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
