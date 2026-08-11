#!/usr/bin/env python3
"""Benchmark workloads across snapshot generation, resume, and warm reuse."""

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
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPResponse
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
BUILD_ARTIFACTS_DIR = SCRIPT_DIR / ".build-artifacts"
HYPERLIGHT_ARTIFACT_CACHE_DIR = BUILD_ARTIFACTS_DIR / "hyperlight"
SAMPLES_DIR = SCRIPT_DIR / "samples"
HELLO_SAMPLE = SAMPLES_DIR / "hello.py"
PANDOC_DOCX_SAMPLE = SAMPLES_DIR / "pandoc_docx.py"
PANDOC_STDLIB_SAMPLE = SAMPLES_DIR / "pandoc_docx_stdlib.py"
PANDOC_NATIVE_SAMPLE = SAMPLES_DIR / "pandoc_native.sh"
NODEJS_HELLO_SAMPLE = SAMPLES_DIR / "nodejs_hello.sh"
NODEJS_RUNTIME_SAMPLE = SAMPLES_DIR / "nodejs_hello.js"
NVX_PYTHON_INITRD = NVX_DIR / "build" / "initramfs-python.cpio.gz"
NVX_NODE_RUNTIME_INITRD = (
    NVX_DIR / "build" / "initramfs-node-runtime-preinitialized.cpio.gz"
)
PYPANDOC_VERSION = "1.17"
PYPANDOC_WHEEL = f"pypandoc-{PYPANDOC_VERSION}-py3-none-any.whl"
NVX_PYPANDOC_WHEEL = NVX_DIR / "alpine" / PYPANDOC_WHEEL
PYPANDOC_WHEEL_SHA256 = (
    "01fdbffa61edb9f8e82e8faad6954efcb7b6f8f0634aead4d89e322a00225a67"
)
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

# Per-workload Hyperlight images from GHCR (each contains /kernel + /initrd.cpio)
HYPERLIGHT_WORKLOAD_IMAGE: dict[str, str] = {
    "hello": "ghcr.io/danbugs/vmm-benchmarks/pyhl:latest",
    "pandoc-docx-stdlib": "ghcr.io/danbugs/vmm-benchmarks/pyhl:latest",
    "pandoc-native": "ghcr.io/danbugs/vmm-benchmarks/pandoc-native:latest",
    "nodejs-hello": "ghcr.io/danbugs/vmm-benchmarks/nodejs:latest",
    "nodejs-compile-test": "ghcr.io/danbugs/vmm-benchmarks/nodejs-compile-test:latest",
}
HYPERLIGHT_APP_ARGS: dict[str, tuple[str, ...]] = {
    "pandoc-native": ("/bin/run-pandoc.sh",),
    "nodejs-hello": ("/app/hello.js",),
    "nodejs-compile-test": ("/app/build-and-test.js",),
}
HYPERLIGHT_EMBEDDED_MARKERS = {
    "pandoc-native": "PANDOC_DOCX_OK",
    "nodejs-hello": "Hello from Node.js on Hyperlight!",
    "nodejs-compile-test": "NODE_TEST_OK",
}
# Workloads using pyhl mode (call_named with script); others use generic (call_run)
HYPERLIGHT_PYHL_WORKLOADS = {"hello", "pandoc-docx-stdlib"}
NVX_PYTHON_WORKLOADS = {"hello", "pandoc-docx-stdlib", "pandoc-docx"}
NVX_MOUNTED_EXEC_WORKLOADS = {"pandoc-native", "nodejs-hello"}

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
WORKLOAD_ORDER = ("hello", "pandoc-docx-stdlib", "pandoc-docx", "pandoc-native", "nodejs-hello", "nodejs-compile-test")
WORKLOAD_LABELS = {
    "hello": "Python hello.py",
    "pandoc-docx-stdlib": "Python stdlib Markdown-to-DOCX",
    "pandoc-docx": "Python + pypandoc Markdown to DOCX",
    "pandoc-native": "Pandoc native Markdown-to-DOCX",
    "nodejs-hello": "Node.js hello",
    "nodejs-compile-test": "Node.js compile-and-test",
}
WORKLOAD_TITLES = {
    "hello": "Python Hello-World",
    "pandoc-docx-stdlib": "Python stdlib Markdown-to-DOCX",
    "pandoc-docx": "Python pypandoc Markdown-to-DOCX",
    "pandoc-native": "Pandoc native Markdown-to-DOCX",
    "nodejs-hello": "Node.js Hello-World",
    "nodejs-compile-test": "Node.js Compile and Test",
}
WORKLOAD_VMMS: dict[str, tuple[str, ...]] = {
    "hello": VMM_ORDER,
    "pandoc-docx-stdlib": ("nvx", "hyperlight"),
    "pandoc-docx": ("nvx",),
    "pandoc-native": ("nvx", "hyperlight"),
    "nodejs-hello": ("nvx", "hyperlight"),
    "nodejs-compile-test": ("hyperlight",),
}
WORKLOAD_SAMPLES: dict[str, Path | None] = {
    "hello": HELLO_SAMPLE,
    "pandoc-docx-stdlib": PANDOC_STDLIB_SAMPLE,
    "pandoc-docx": PANDOC_DOCX_SAMPLE,
    "pandoc-native": PANDOC_NATIVE_SAMPLE,
    "nodejs-hello": NODEJS_HELLO_SAMPLE,
    "nodejs-compile-test": None,
}
WORKLOAD_SAMPLE_VMMS: dict[str, tuple[str, ...]] = {
    "hello": VMM_ORDER,
    "pandoc-docx-stdlib": ("nvx", "hyperlight"),
    "pandoc-docx": ("nvx",),
    "pandoc-native": ("nvx",),
    "nodejs-hello": ("nvx",),
    "nodejs-compile-test": (),
}
WORKLOAD_MARKERS = {
    "hello": "hello world",
    "pandoc-docx-stdlib": "PANDOC_DOCX_OK",
    "pandoc-docx": "PANDOC_DOCX_OK",
    "pandoc-native": "PANDOC_DOCX_OK",
    "nodejs-hello": "hello",
    "nodejs-compile-test": "NODE_TEST_OK",
}
RSS_VMM_ORDER = ("nanvix", "nvx", "hyperlight")
GUEST_MEMORY_MIB: dict[str, dict[str, int]] = {
    "hello": {
        "nvx": 1536,
        "nanvix": 256,
        "hyperlight": 2560,
    },
    "pandoc-docx-stdlib": {
        "nvx": 1536,
        "hyperlight": 2560,
    },
    "pandoc-docx": {
        "nvx": 1536,
    },
    "pandoc-native": {
        "nvx": 1536,
        "hyperlight": 2560,
    },
    "nodejs-hello": {
        "nvx": 1536,
        "hyperlight": 512,
    },
    "nodejs-compile-test": {
        "hyperlight": 512,
    },
}
MODE_ORDER = (
    "snapshot-generation",
    "restore",
    "runtime-preinitialized",
    "warm",
)
SNAPSHOT_MODE_ORDER = ("snapshot-generation", "restore", "warm")
LEGACY_MODE_ORDER = ("cold", "restore", "warm")
MODE_LABELS = {
    "snapshot-generation": "Snapshot Generation",
    "cold": "Cold Start",
    "restore": "Persisted Snapshot Resume",
    "runtime-preinitialized": "Runtime-Preinitialized Resume",
    "warm": "Warm Reuse",
}
SNAPSHOT_GENERATION_SAMPLES = 1
GENERIC_SNAPSHOT_WARMUP = "pass"
MANIFEST_FORMAT = 5
HYPERLIGHT_SNAPSHOT_FORMAT = 3
HYPERLIGHT_ARTIFACT_CACHE_FORMAT = 1
OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
OCI_MANIFEST_ACCEPT = ", ".join(
    [*sorted(OCI_INDEX_MEDIA_TYPES), *sorted(OCI_MANIFEST_MEDIA_TYPES)]
)
PHASE_FIELDS = (
    "sandbox_build_ms",
    "initial_rewind_ms",
    "snapshot_capture_ms",
    "snapshot_persist_ms",
    "snapshot_load_ms",
    "guest_call_ms",
    "rewind_ms",
    "lifecycle_overhead_ms",
)
PHASE_LABELS = {
    "sandbox_build_ms": "Sandbox build/evolve",
    "initial_rewind_ms": "Initial in-memory rewind",
    "snapshot_capture_ms": "In-memory snapshot capture",
    "snapshot_persist_ms": "Snapshot persistence",
    "snapshot_load_ms": "Persisted snapshot load + VM construction",
    "guest_call_ms": "Guest invocation / snapshot warmup",
    "rewind_ms": "State reset / isolation",
    "lifecycle_overhead_ms": "Remaining process lifecycle",
}
NVX_BASE_CMDLINE = (
    "earlycon=xe9 console=hvc0 quiet loglevel=0 reboot=t panic=-1"
)
NVX_PYTHON_CMDLINE = (
    "earlycon=xe9 console=hvc0 quiet loglevel=0 reboot=t panic=-1 "
    "nvx_mode=hostfs nvx_snapshot=1 pyapp=app.py"
)
NVX_WARMUP_IMPORTS = ("numpy", "pandas", "pypandoc")
NVX_WARM_ITERATIONS_FILE = ".vmm-benchmark-iterations"
NVX_NODE_RUNTIME_MEMORY_MIB = 512
NVX_NODE_RUNTIME_CMDLINE = (
    "earlycon=xe9 console=hvc0 quiet loglevel=0 reboot=t panic=-1 "
    "nvx_mode=node-runtime-preinitialized"
)
NVX_NODE_RUNTIME_MARKER = "NVX-RUNTIME-PREINITIALIZED-START"
NVX_NODE_RUNTIME_REQUEST_MODE = "runtime-preinitialized"
NVX_NODE_RUNTIME_PROFILE_RUNS = 5
NVX_NODE_RUNTIME_PROFILE_MARKER = "NVX-NODE-RUNTIME-PROFILE-OK"


def nvx_cmdline(workload: str) -> str:
    if workload == "pandoc-docx":
        return f"{NVX_PYTHON_CMDLINE} nvx_python_warmup=1"
    return NVX_PYTHON_CMDLINE


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
    success_marker: str | None
    iterations_file: Path | None = None
    reset_path: Path | None = None
    success_paths: tuple[Path, ...] = ()
    request_script: Path | None = None
    additional_success_markers: tuple[str, ...] = ()

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


def manifest_workload(manifest: dict[str, object]) -> str:
    workload_record = manifest.get("workload")
    if isinstance(workload_record, dict):
        workload_id = cast(dict[str, object], workload_record).get("id")
        if isinstance(workload_id, str) and workload_id in WORKLOAD_ORDER:
            return workload_id
    return "hello"


def manifest_mode_order(manifest: dict[str, object]) -> tuple[str, ...]:
    manifest_format = manifest.get("format")
    if manifest_format in {4, MANIFEST_FORMAT}:
        return MODE_ORDER
    if manifest_format == 3:
        return SNAPSHOT_MODE_ORDER
    return LEGACY_MODE_ORDER


def configured_snapshot_generation_samples(
    manifest: dict[str, object],
) -> int | None:
    if manifest.get("format") in {4, MANIFEST_FORMAT}:
        return SNAPSHOT_GENERATION_SAMPLES
    snapshot_policy = manifest.get("snapshot_policy")
    if not isinstance(snapshot_policy, dict):
        return None
    generation = snapshot_policy.get("snapshot_generation")
    if not isinstance(generation, dict):
        return None
    samples = generation.get("samples_per_vmm_workload")
    return samples if isinstance(samples, int) and samples > 0 else None


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
            "rerun with --build --use-docker"
        )


def git_apply_check(repository: Path, patch: Path, *, reverse: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        "git",
        "apply",
        "--ignore-space-change",
        "--unidiff-zero",
        "--check",
    ]
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
            ["git", "apply", "--ignore-space-change", "--unidiff-zero", patch],
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
                [
                    "git",
                    "apply",
                    "--ignore-space-change",
                    "--unidiff-zero",
                    "--reverse",
                    patch,
                ],
                cwd=repository,
            )


def parse_oci_image(image: str) -> tuple[str, str, str]:
    registry, separator, repository_reference = image.partition("/")
    if not separator or not registry or not repository_reference:
        raise RuntimeError(f"OCI image must include a registry and repository: {image}")
    if "@" in repository_reference:
        repository, reference = repository_reference.rsplit("@", 1)
    else:
        repository, tag_separator, reference = repository_reference.rpartition(":")
        if not tag_separator or "/" in reference:
            repository = repository_reference
            reference = "latest"
    if not repository or not reference:
        raise RuntimeError(f"invalid OCI image reference: {image}")
    return registry, repository, reference


def verify_sha256_digest(data: bytes, digest: str, subject: str) -> None:
    algorithm, separator, expected = digest.partition(":")
    if separator != ":" or algorithm != "sha256" or not expected:
        raise RuntimeError(f"unsupported digest for {subject}: {digest}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 mismatch for {subject}: expected {expected}, got {actual}"
        )


class OciRegistryClient:
    def __init__(self, registry: str, repository: str) -> None:
        self.registry = registry
        self.repository = repository
        self.authorization: str | None = None

    def _url(self, path: str) -> str:
        return f"https://{self.registry}/v2/{self.repository}/{path}"

    @staticmethod
    def _open(request: urllib.request.Request) -> HTTPResponse:
        try:
            return cast(HTTPResponse, urllib.request.urlopen(request, timeout=120))
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as error:
            raise RuntimeError(f"OCI registry request failed: {error}") from error

    def _authorize(self, challenge: str) -> None:
        scheme, separator, parameters = challenge.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise RuntimeError(
                f"OCI registry returned an unsupported authentication challenge: {challenge}"
            )
        values = dict(re.findall(r'(\w+)="([^"]*)"', parameters))
        realm = values.get("realm")
        service = values.get("service")
        scope = values.get("scope", f"repository:{self.repository}:pull")
        if not realm or not service or not realm.startswith("https://"):
            raise RuntimeError("OCI registry returned an invalid bearer challenge")
        token_query = urllib.parse.urlencode(
            {
                "service": service,
                "scope": scope,
            }
        )
        token_url = f"{realm}?{token_query}"
        request = urllib.request.Request(
            token_url,
            headers={"User-Agent": "vmm-benchmarks"},
        )
        try:
            with self._open(request) as response:
                payload: object = json.loads(response.read())
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError("OCI registry returned an invalid token response") from error
        if not isinstance(payload, dict):
            raise RuntimeError("OCI registry returned an invalid token response")
        token = payload.get("token", payload.get("access_token"))
        if not isinstance(token, str) or not token:
            raise RuntimeError("OCI registry token response did not include a token")
        self.authorization = f"Bearer {token}"

    def request(self, path: str, *, accept: str | None = None) -> HTTPResponse:
        headers = {"User-Agent": "vmm-benchmarks"}
        if accept is not None:
            headers["Accept"] = accept
        if self.authorization is not None:
            headers["Authorization"] = self.authorization
        request = urllib.request.Request(self._url(path), headers=headers)
        try:
            return self._open(request)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise RuntimeError(
                    f"OCI registry request failed with HTTP {error.code}: {self._url(path)}"
                ) from error
            challenge = error.headers.get("WWW-Authenticate", "")
            error.close()
            self._authorize(challenge)

        headers["Authorization"] = cast(str, self.authorization)
        retry = urllib.request.Request(self._url(path), headers=headers)
        try:
            return self._open(retry)
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"OCI registry request failed with HTTP {error.code}: {self._url(path)}"
            ) from error

    def manifest(self, reference: str) -> dict[str, object]:
        quoted_reference = urllib.parse.quote(reference, safe=":")
        with self.request(
            f"manifests/{quoted_reference}",
            accept=OCI_MANIFEST_ACCEPT,
        ) as response:
            body = response.read()
            digest = response.headers.get("Docker-Content-Digest")
        if digest is not None:
            verify_sha256_digest(body, digest, f"manifest {reference}")
        try:
            manifest: object = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError(
                f"OCI registry returned an invalid manifest for {reference}"
            ) from error
        if not isinstance(manifest, dict):
            raise RuntimeError(f"OCI manifest for {reference} is not an object")
        return cast(dict[str, object], manifest)

    def download_blob(self, digest: str, destination: Path) -> None:
        algorithm, separator, expected = digest.partition(":")
        if separator != ":" or algorithm != "sha256" or not expected:
            raise RuntimeError(f"unsupported OCI blob digest: {digest}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        quoted_digest = urllib.parse.quote(digest, safe=":")
        with (
            self.request(f"blobs/{quoted_digest}") as response,
            destination.open("wb") as output,
        ):
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if actual != expected:
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA-256 mismatch for OCI blob {digest}: got {actual}"
            )


def resolve_oci_layers(
    client: OciRegistryClient,
    reference: str,
) -> list[dict[str, object]]:
    manifest = client.manifest(reference)
    media_type = manifest.get("mediaType")
    if media_type in OCI_INDEX_MEDIA_TYPES:
        manifests = manifest.get("manifests")
        if not isinstance(manifests, list):
            raise RuntimeError("OCI image index does not contain manifests")
        selected: dict[str, object] | None = None
        for candidate in manifests:
            if not isinstance(candidate, dict):
                continue
            platform_value = candidate.get("platform")
            if not isinstance(platform_value, dict):
                continue
            if (
                platform_value.get("os") == "linux"
                and platform_value.get("architecture") == "amd64"
            ):
                selected = cast(dict[str, object], candidate)
                break
        if selected is None:
            raise RuntimeError("OCI image has no linux/amd64 manifest")
        digest = selected.get("digest")
        if not isinstance(digest, str):
            raise RuntimeError("OCI linux/amd64 manifest has no digest")
        manifest = client.manifest(digest)
        media_type = manifest.get("mediaType")
    if media_type not in OCI_MANIFEST_MEDIA_TYPES:
        raise RuntimeError(f"unsupported OCI manifest media type: {media_type!r}")
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        raise RuntimeError("OCI image manifest does not contain layers")
    if not all(isinstance(layer, dict) for layer in layers):
        raise RuntimeError("OCI image manifest contains an invalid layer")
    return cast(list[dict[str, object]], layers)


def extract_oci_artifacts(
    image: str,
    artifacts: dict[str, Path],
) -> None:
    def normalize_path(path: str) -> str:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized.lstrip("/")

    registry, repository, reference = parse_oci_image(image)
    client = OciRegistryClient(registry, repository)
    layers = resolve_oci_layers(client, reference)
    normalized_artifacts = {
        normalize_path(source): destination
        for source, destination in artifacts.items()
    }
    whiteouts: dict[str, Path] = {}
    for source, destination in normalized_artifacts.items():
        parent, separator, name = source.rpartition("/")
        whiteout = f"{parent}{separator}.wh.{name}"
        whiteouts[whiteout] = destination
    for destination in artifacts.values():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)

    print(f"+ (OCI) pull {image}", flush=True)
    HYPERLIGHT_ARTIFACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="hyperlight-oci-",
        dir=HYPERLIGHT_ARTIFACT_CACHE_DIR,
    ) as temporary_dir:
        temporary = Path(temporary_dir)
        for index, layer in enumerate(layers):
            digest = layer.get("digest")
            media_type = layer.get("mediaType")
            if not isinstance(digest, str) or not isinstance(media_type, str):
                raise RuntimeError("OCI image manifest contains an invalid layer")
            if "zstd" in media_type:
                raise RuntimeError(
                    "zstd-compressed OCI layers require --use-docker"
                )
            blob = temporary / f"layer-{index}.tar"
            client.download_blob(digest, blob)
            try:
                with tarfile.open(blob, mode="r:*") as archive:
                    for member in archive:
                        name = normalize_path(member.name)
                        whiteout_destination = whiteouts.get(name)
                        if whiteout_destination is not None:
                            whiteout_destination.unlink(missing_ok=True)
                            continue
                        destination = normalized_artifacts.get(name)
                        if destination is None:
                            continue
                        if not member.isfile():
                            raise RuntimeError(
                                f"OCI artifact is not a regular file: /{name}"
                            )
                        source_file = archive.extractfile(member)
                        if source_file is None:
                            raise RuntimeError(
                                f"cannot read OCI artifact from layer: /{name}"
                            )
                        with source_file, destination.open("wb") as output:
                            shutil.copyfileobj(source_file, output)
            except tarfile.TarError as error:
                raise RuntimeError(
                    f"cannot read OCI layer {digest}: {error}"
                ) from error
            blob.unlink()

    missing = [
        source
        for source, destination in artifacts.items()
        if not destination.is_file()
    ]
    if missing:
        raise RuntimeError(
            f"OCI image {image} does not contain: {', '.join(missing)}"
        )


def remove_docker_container(docker: str, name: str) -> None:
    subprocess.run(
        [docker, "rm", "-f", name],
        cwd=SCRIPT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def extract_docker_artifacts(
    docker: str,
    image: str,
    artifacts: dict[str, Path],
) -> None:
    container_key = hashlib.sha256(image.encode("utf-8")).hexdigest()[:12]
    container = f"vmm-benchmark-{container_key}-{os.getpid()}"
    for destination in artifacts.values():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
    run_checked([docker, "pull", image], cwd=SCRIPT_DIR)
    remove_docker_container(docker, container)
    try:
        first_source = next(iter(artifacts))
        run_checked(
            [docker, "create", "--name", container, image, first_source],
            cwd=SCRIPT_DIR,
        )
        for source, destination in artifacts.items():
            run_checked(
                [docker, "cp", f"{container}:{source}", destination],
                cwd=SCRIPT_DIR,
            )
    finally:
        remove_docker_container(docker, container)
    for destination in artifacts.values():
        require_file(destination)


def hyperlight_artifact_cache_dir(image: str) -> Path:
    image_key = hashlib.sha256(image.encode("utf-8")).hexdigest()
    return HYPERLIGHT_ARTIFACT_CACHE_DIR / image_key


def cached_hyperlight_artifacts(image: str) -> tuple[Path, Path] | None:
    cache = hyperlight_artifact_cache_dir(image)
    kernel = cache / "kernel"
    initrd = cache / "initrd.cpio"
    config_path = cache / "artifact-config.json"
    if not kernel.is_file() or not initrd.is_file() or not config_path.is_file():
        return None
    try:
        config: object = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "format": HYPERLIGHT_ARTIFACT_CACHE_FORMAT,
        "image": image,
        "kernel_sha256": sha256(kernel),
        "initrd_sha256": sha256(initrd),
    }
    return (kernel, initrd) if config == expected else None


def cache_hyperlight_artifacts(
    image: str,
    kernel: Path,
    initrd: Path,
) -> tuple[Path, Path]:
    cache = hyperlight_artifact_cache_dir(image)
    cache.mkdir(parents=True, exist_ok=True)
    cached_kernel = cache / "kernel"
    cached_initrd = cache / "initrd.cpio"
    shutil.copyfile(require_file(kernel), cached_kernel)
    shutil.copyfile(require_file(initrd), cached_initrd)
    config = {
        "format": HYPERLIGHT_ARTIFACT_CACHE_FORMAT,
        "image": image,
        "kernel_sha256": sha256(cached_kernel),
        "initrd_sha256": sha256(cached_initrd),
    }
    (cache / "artifact-config.json").write_text(
        json.dumps(config, indent=2),
        encoding="utf-8",
    )
    return cached_kernel, cached_initrd


def stage_hyperlight_artifacts(
    image: str,
    kernel: Path,
    initrd: Path,
    *,
    use_docker: bool,
) -> str:
    if kernel.is_file() and initrd.is_file():
        return "existing output"

    cached = cached_hyperlight_artifacts(image)
    acquisition = "persistent cache"
    if cached is None:
        HYPERLIGHT_ARTIFACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="hyperlight-artifacts-",
            dir=HYPERLIGHT_ARTIFACT_CACHE_DIR,
        ) as temporary_dir:
            temporary = Path(temporary_dir)
            extracted_kernel = temporary / "kernel"
            extracted_initrd = temporary / "initrd.cpio"
            extracted = {
                "/kernel": extracted_kernel,
                "/initrd.cpio": extracted_initrd,
            }
            if use_docker:
                extract_docker_artifacts(
                    require_command("docker"),
                    image,
                    extracted,
                )
                acquisition = "Docker image"
            else:
                extract_oci_artifacts(image, extracted)
                acquisition = "OCI registry"
            cached = cache_hyperlight_artifacts(
                image,
                extracted_kernel,
                extracted_initrd,
            )

    cached_kernel, cached_initrd = cached
    kernel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached_kernel, kernel)
    shutil.copyfile(cached_initrd, initrd)
    return acquisition


def stage_sample(
    output_dir: Path,
    artifacts: Path,
    source: Path,
    *,
    allow_initial_with_results: bool = False,
) -> Path:
    source = require_file(source)
    staged = artifacts / "samples" / source.name
    source_bytes = source.read_bytes()
    staged_exists = staged.is_file()
    changed = not staged_exists or staged.read_bytes() != source_bytes
    if (
        changed
        and (output_dir / "raw.csv").is_file()
        and not (allow_initial_with_results and not staged_exists)
    ):
        raise RuntimeError(
            f"benchmark sample changed for existing results in {output_dir}; "
            "choose a new --output directory"
        )
    staged.parent.mkdir(parents=True, exist_ok=True)
    if changed:
        shutil.copyfile(source, staged)
    return staged


def verify_pypandoc_wheel(wheel: Path) -> None:
    wheel_sha256 = sha256(require_file(wheel))
    if wheel_sha256 != PYPANDOC_WHEEL_SHA256:
        raise RuntimeError(f"unexpected SHA-256 for {PYPANDOC_WHEEL}: {wheel_sha256}")


@contextmanager
def temporary_pypandoc_wheel() -> Generator[None, None, None]:
    if NVX_PYPANDOC_WHEEL.is_file():
        verify_pypandoc_wheel(NVX_PYPANDOC_WHEEL)
        yield
        return
    with tempfile.TemporaryDirectory(prefix="vmm-benchmark-pypandoc-") as wheel_dir_name:
        wheel_dir = Path(wheel_dir_name)
        run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-cache-dir",
                "--no-deps",
                "--only-binary=:all:",
                "--dest",
                wheel_dir,
                f"pypandoc=={PYPANDOC_VERSION}",
            ],
            cwd=SCRIPT_DIR,
        )
        wheel = require_file(wheel_dir / PYPANDOC_WHEEL)
        verify_pypandoc_wheel(wheel)
        shutil.copyfile(wheel, NVX_PYPANDOC_WHEEL)
        try:
            yield
        finally:
            NVX_PYPANDOC_WHEEL.unlink(missing_ok=True)


def git_head(repository: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def nvx_build_provenance() -> dict[str, str]:
    return {
        "nvx_commit": git_head(NVX_DIR),
        "build_patch_sha256": sha256(NVX_BUILD_PATCH),
        "pandoc_version": "3.10",
        "pypandoc_version": PYPANDOC_VERSION,
        "pypandoc_wheel_sha256": PYPANDOC_WHEEL_SHA256,
        "node_runtime_mode": NVX_NODE_RUNTIME_REQUEST_MODE,
        "node_runtime_profile_runs": str(NVX_NODE_RUNTIME_PROFILE_RUNS),
    }


def build_projects(selected: set[str], *, use_docker: bool = False) -> None:
    docker_builds = selected.intersection({"nvx", "nanvix"})
    if docker_builds and not use_docker:
        targets = ", ".join(sorted(docker_builds))
        raise RuntimeError(
            f"--build for {targets} requires Docker; rerun with --use-docker"
        )
    if docker_builds:
        require_command("docker")
    if selected.intersection({"nvx", "hyperlight"}):
        require_command("cargo")

    if "nvx" in selected:
        (BUILD_RECEIPTS_DIR / "nvx.json").unlink(missing_ok=True)
        (BUILD_RECEIPTS_DIR / "nvx-pandoc-docx.json").unlink(missing_ok=True)
        with temporary_submodule_patch(
            NVX_DIR, NVX_BUILD_PATCH
        ), temporary_pypandoc_wheel():
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
            run_checked(
                [
                    sys.executable,
                    "scripts\\nvx.py",
                    "build-python-initramfs",
                    "--profile",
                    "node-runtime-preinitialized",
                    "--docker",
                    "--dest",
                    "build",
                ],
                cwd=NVX_DIR,
            )
            run_checked(["cargo", "build", "--release"], cwd=NVX_DIR)
            write_build_receipt(
                "nvx",
                nvx_build_provenance(),
                {
                    "vmm": NVX_DIR / "target" / "release" / "microvm.exe",
                    "kernel": NVX_DIR / "build" / "vmlinux",
                    "python_initrd": NVX_PYTHON_INITRD,
                    "node_runtime_initrd": NVX_NODE_RUNTIME_INITRD,
                },
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
        run_checked(["cargo", "build", "--release"], cwd=HYPERLIGHT_RUNNER_DIR)


def run_control(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    timeout: float,
    marker: str | None = None,
    input_bytes: bytes | None = None,
) -> str:
    rendered = [str(item) for item in command]
    print(f"+ ({cwd}) {command_text(rendered)}", flush=True)
    completed = subprocess.run(
        rendered,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        input=input_bytes,
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


def remove_measurement_artifact(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def encode_node_runtime_request(script: str) -> bytes:
    document = {
        "mode": NVX_NODE_RUNTIME_REQUEST_MODE,
        "env": [],
        "argv": [],
        "entropy": list(os.urandom(32)),
        "unixTimeNs": time.time_ns(),
        "script": script,
    }
    body = json.dumps(document, separators=(",", ":")).encode("utf-8")
    if len(body) > 16 * 1024 * 1024:
        raise ValueError("runtime-preinitialized Node.js request exceeds 16 MiB")
    return len(body).to_bytes(8, "big") + body


def build_node_runtime_request(script_path: Path) -> bytes:
    return encode_node_runtime_request(
        require_file(script_path).read_text(encoding="utf-8")
    )


def node_profile_training_runs(output: str) -> int:
    for line in reversed(output.splitlines()):
        if "snapshot-hot-pages: status=trained" not in line:
            continue
        for field in line.split():
            if field.startswith("training_runs="):
                return int(field.partition("=")[2])
    raise RuntimeError("NVX hot-profile training did not report its run count")


def measure(spec: CommandSpec, timeout: float) -> Measurement:
    if spec.reset_path is not None:
        remove_measurement_artifact(spec.reset_path)
    try:
        return measure_process(spec, timeout)
    finally:
        if spec.reset_path is not None:
            remove_measurement_artifact(spec.reset_path)


def measure_process(spec: CommandSpec, timeout: float) -> Measurement:
    request = (
        build_node_runtime_request(spec.request_script)
        if spec.request_script is not None
        else None
    )
    started = time.perf_counter_ns()
    process = subprocess.Popen(
        spec.command,
        cwd=spec.cwd,
        stdin=subprocess.PIPE if request is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output_bytes, _ = process.communicate(input=request, timeout=timeout)
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
    missing_paths = [
        path
        for path in spec.success_paths
        if not path.is_file() or path.stat().st_size == 0
    ]
    if missing_paths:
        missing = ", ".join(str(path) for path in missing_paths)
        raise RuntimeError(
            f"{spec.vmm}/{spec.mode} did not create required artifact(s): {missing}\n"
            f"{output}"
        )
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
    workload: str,
    *,
    use_docker: bool = False,
) -> dict[tuple[str, str], CommandSpec]:
    artifacts = output_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    raw_path = output_dir / "raw.csv"
    previous_manifest: dict[str, object] = {}
    if raw_path.is_file() and not manifest_path.is_file():
        raise RuntimeError(
            f"raw.csv in {output_dir} has no manifest.json; "
            "choose a new --output directory"
        )
    if manifest_path.is_file():
        try:
            loaded_manifest: object = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"manifest.json in {output_dir} is unreadable; "
                "choose a new --output directory"
            ) from error
        if not isinstance(loaded_manifest, dict):
            raise RuntimeError(
                f"manifest.json in {output_dir} is not an object; "
                "choose a new --output directory"
            )
        previous_manifest = cast(dict[str, object], loaded_manifest)
        previous_workload = manifest_workload(previous_manifest)
        if previous_workload != workload:
            raise RuntimeError(
                f"existing results in {output_dir} use workload "
                f"{previous_workload!r}, not {workload!r}; choose a new "
                "--output directory"
            )
        if previous_manifest.get("format") != MANIFEST_FORMAT:
            raise RuntimeError(
                f"existing results in {output_dir} use an incompatible manifest "
                "format; choose a new --output directory"
            )
        if raw_path.is_file():
            previous_harness = previous_manifest.get("harness")
            if (
                not isinstance(previous_harness, dict)
                or previous_harness.get("benchmark_sha256")
                != sha256(Path(__file__))
                or previous_harness.get("repository_commit")
                != git_head(SCRIPT_DIR)
            ):
                raise RuntimeError(
                    f"benchmark harness provenance changed since results were "
                    f"recorded in {output_dir}; choose a new --output directory"
                )
            metadata_path = output_dir / "metadata.json"
            if not metadata_path.is_file():
                raise RuntimeError(
                    f"raw.csv in {output_dir} has no metadata.json; "
                    "choose a new --output directory"
                )
            try:
                previous_metadata: object = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"metadata.json in {output_dir} is unreadable; "
                    "choose a new --output directory"
                ) from error
            if not isinstance(previous_metadata, dict):
                raise RuntimeError(
                    f"metadata.json in {output_dir} is not an object; "
                    "choose a new --output directory"
                )
            previous_repositories = previous_metadata.get("repositories")
            repository_paths = {
                "vmm-benchmarks": SCRIPT_DIR,
                "nvx": NVX_DIR,
                "nanvix-python": NANVIX_DIR,
                "hyperlight-unikraft": HYPERLIGHT_DIR,
            }
            if not isinstance(previous_repositories, dict):
                raise RuntimeError(
                    f"metadata.json in {output_dir} has no repository provenance; "
                    "choose a new --output directory"
                )
            changed_repositories = [
                name
                for name, repository in repository_paths.items()
                if (
                    not isinstance(previous_repositories.get(name), dict)
                    or previous_repositories[name].get("commit")
                    != git_head(repository)
                )
            ]
            if changed_repositories:
                changed = ", ".join(changed_repositories)
                raise RuntimeError(
                    f"repository revision changed for {changed} since results "
                    f"were recorded in {output_dir}; choose a new --output directory"
                )
    previous_commands_value = previous_manifest.get("commands")
    previous_vmms = (
        {
            key.partition("/")[0]
            for key in previous_commands_value
            if isinstance(key, str) and key.partition("/")[0] in VMM_ORDER
        }
        if isinstance(previous_commands_value, dict)
        else set()
    )
    workload_sample_source = WORKLOAD_SAMPLES[workload]
    sample_vmms = set(WORKLOAD_SAMPLE_VMMS[workload])
    sample: Path | None = (
        stage_sample(
            output_dir,
            artifacts,
            workload_sample_source,
            allow_initial_with_results=(
                bool(previous_vmms) and not previous_vmms.intersection(sample_vmms)
            ),
        )
        if (
            workload_sample_source is not None
            and (selected | previous_vmms).intersection(sample_vmms)
        )
        else None
    )
    runtime_sample: Path | None = (
        stage_sample(
            output_dir,
            artifacts,
            NODEJS_RUNTIME_SAMPLE,
            allow_initial_with_results=(
                bool(previous_vmms) and "nvx" not in previous_vmms
            ),
        )
        if workload == "nodejs-hello"
        and ("nvx" in selected or "nvx" in previous_vmms)
        else None
    )
    memory = GUEST_MEMORY_MIB[workload]
    marker = WORKLOAD_MARKERS[workload]
    warmup_imports = (
        list(NVX_WARMUP_IMPORTS)
        if "nvx" in selected and workload == "pandoc-docx"
        else []
    )
    specs: dict[tuple[str, str], CommandSpec] = {}
    hyperlight_artifact_provenance: dict[str, object] | None = None
    node_runtime_profile_provenance: dict[str, object] | None = None

    if "nvx" in selected:
        assert sample is not None
        nvx_work = artifacts / "nvx"
        nvx_mount = nvx_work / "mnt"
        nvx_warm_mount = nvx_work / "warm-mnt"
        nvx_snapshot = nvx_work / "snapshot"
        nvx_generation_snapshot = nvx_work / "snapshot-generation"
        nvx_mount.mkdir(parents=True, exist_ok=True)
        nvx_snapshot.mkdir(parents=True, exist_ok=True)
        nvx_is_python = workload in NVX_PYTHON_WORKLOADS
        if not nvx_is_python and workload not in NVX_MOUNTED_EXEC_WORKLOADS:
            raise RuntimeError(f"unsupported NVX workload: {workload}")
        nvx_app_name = "app.py" if nvx_is_python else "app.sh"
        nvx_guest_app = f"/mnt/host/{nvx_app_name}"
        shutil.copyfile(sample, nvx_mount / nvx_app_name)
        if nvx_is_python:
            nvx_warm_mount.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(sample, nvx_warm_mount / nvx_app_name)

        executable = require_file(NVX_DIR / "target" / "release" / "microvm.exe")
        kernel = require_file(NVX_DIR / "build" / "vmlinux")
        initrd = require_file(NVX_PYTHON_INITRD)
        node_runtime_initrd = require_file(NVX_NODE_RUNTIME_INITRD)
        require_build_receipt(
            "nvx",
            nvx_build_provenance(),
            {
                "vmm": executable,
                "kernel": kernel,
                "python_initrd": initrd,
                "node_runtime_initrd": node_runtime_initrd,
            },
        )
        cmdline = nvx_cmdline(workload) if nvx_is_python else NVX_BASE_CMDLINE
        nvx_snapshot_config_path = nvx_snapshot / "benchmark-config.json"
        expected_nvx_snapshot_config: dict[str, object] = {
            "format": 2,
            "vmm_sha256": sha256(executable),
            "kernel_sha256": sha256(kernel),
            "initrd_sha256": sha256(initrd),
            "guest_memory_mib": memory["nvx"],
            "build_patch_sha256": sha256(NVX_BUILD_PATCH),
            "workload": workload,
            "cmdline": cmdline,
            "execution": "python-trampoline" if nvx_is_python else "mounted-exec",
            "sample_sha256": sha256(sample),
            "warmup": {
                "statement": GENERIC_SNAPSHOT_WARMUP if nvx_is_python else None,
                "imports": warmup_imports,
            },
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
            if (output_dir / "raw.csv").is_file() and "nvx" in previous_vmms:
                raise RuntimeError(
                    "NVX snapshot does not match the current workload image "
                    f"in {output_dir}; choose a new --output directory"
                )
            shutil.rmtree(nvx_snapshot, ignore_errors=True)
            nvx_snapshot.mkdir(parents=True)
            capture_command: list[str | Path] = [
                executable,
                "--kernel",
                kernel,
                "--initrd",
                initrd,
                "--mem",
                str(memory["nvx"]),
                "--cmdline",
                cmdline,
                "--mount",
                nvx_mount,
                "--snapshot",
                nvx_snapshot,
                "--quiet",
            ]
            if not nvx_is_python:
                capture_command.extend(
                    ["--exec", nvx_guest_app, "--snapshot-before-exec"]
                )
            run_control(
                capture_command,
                cwd=NVX_DIR,
                timeout=timeout,
            )
            nvx_snapshot_config_path.write_text(
                json.dumps(expected_nvx_snapshot_config, indent=2),
                encoding="utf-8",
            )
        require_file(nvx_snapshot / "state.bin")
        require_file(nvx_snapshot / "mem.bin")

        if workload == "nodejs-hello":
            assert runtime_sample is not None
            node_runtime_snapshot = nvx_work / "runtime-preinitialized-snapshot"
            node_runtime_config_path = (
                node_runtime_snapshot / "benchmark-config.json"
            )
            node_runtime_profile_path = (
                node_runtime_snapshot / "hot-pages.whp.v1"
            )
            expected_node_runtime_config: dict[str, object] = {
                "format": 1,
                "vmm_sha256": sha256(executable),
                "kernel_sha256": sha256(kernel),
                "initrd_sha256": sha256(node_runtime_initrd),
                "guest_memory_mib": NVX_NODE_RUNTIME_MEMORY_MIB,
                "build_patch_sha256": sha256(NVX_BUILD_PATCH),
                "mode": NVX_NODE_RUNTIME_REQUEST_MODE,
                "cmdline": NVX_NODE_RUNTIME_CMDLINE,
                "sample_sha256": sha256(runtime_sample),
                "requested_hot_profile_training_runs": (
                    NVX_NODE_RUNTIME_PROFILE_RUNS
                ),
                "hot_profile_marker": NVX_NODE_RUNTIME_PROFILE_MARKER,
                "workload_in_snapshot": False,
            }
            node_runtime_config: object = None
            if node_runtime_config_path.is_file():
                node_runtime_config = json.loads(
                    node_runtime_config_path.read_text(encoding="utf-8")
                )
            config_matches = (
                isinstance(node_runtime_config, dict)
                and all(
                    node_runtime_config.get(key) == value
                    for key, value in expected_node_runtime_config.items()
                )
            )
            profile_record = (
                node_runtime_config.get("hot_profile")
                if isinstance(node_runtime_config, dict)
                else None
            )
            profile_matches = (
                node_runtime_profile_path.is_file()
                and isinstance(profile_record, dict)
                and isinstance(profile_record.get("training_runs"), int)
                and profile_record["training_runs"]
                >= NVX_NODE_RUNTIME_PROFILE_RUNS
                and profile_record.get("sha256")
                == sha256(node_runtime_profile_path)
            )
            node_runtime_matches = (
                (node_runtime_snapshot / "state.bin").is_file()
                and (node_runtime_snapshot / "mem.bin").is_file()
                and config_matches
                and profile_matches
            )
            if not node_runtime_matches:
                if (output_dir / "raw.csv").is_file() and "nvx" in previous_vmms:
                    raise RuntimeError(
                        "NVX runtime-preinitialized snapshot does not match "
                        f"{output_dir}; choose a new --output directory"
                    )
                shutil.rmtree(node_runtime_snapshot, ignore_errors=True)
                node_runtime_snapshot.mkdir(parents=True)
                run_control(
                    [
                        executable,
                        "--kernel",
                        kernel,
                        "--initrd",
                        node_runtime_initrd,
                        "--mem",
                        str(NVX_NODE_RUNTIME_MEMORY_MIB),
                        "--cmdline",
                        NVX_NODE_RUNTIME_CMDLINE,
                        "--snapshot",
                        node_runtime_snapshot,
                        "--console",
                        "virtio",
                        "--quiet",
                    ],
                    cwd=NVX_DIR,
                    timeout=timeout,
                )
                successful_training_runs = 0
                reported_training_runs = 0
                max_training_attempts = NVX_NODE_RUNTIME_PROFILE_RUNS + 2
                for attempt in range(1, max_training_attempts + 1):
                    try:
                        training_output = run_control(
                            [
                                executable,
                                "--restore",
                                node_runtime_snapshot,
                                "--mem",
                                str(NVX_NODE_RUNTIME_MEMORY_MIB),
                                "--console",
                                "virtio",
                                "--exit-on-boot",
                                "--quiet",
                                "--boot-marker",
                                NVX_NODE_RUNTIME_PROFILE_MARKER,
                                "--snapshot-prefetch",
                                "off",
                                "--snapshot-profile-generate",
                                "--log-level",
                                "info",
                            ],
                            cwd=NVX_DIR,
                            timeout=min(timeout, 30.0),
                            input_bytes=encode_node_runtime_request(
                                f'console.log("{NVX_NODE_RUNTIME_PROFILE_MARKER}");'
                            ),
                        )
                    except subprocess.TimeoutExpired:
                        print(
                            "NVX runtime hot-profile training timed out "
                            f"(attempt {attempt}/{max_training_attempts}); retrying",
                            flush=True,
                        )
                        if attempt == max_training_attempts:
                            raise
                        continue
                    reported_training_runs = node_profile_training_runs(
                        training_output
                    )
                    successful_training_runs += 1
                    if (
                        successful_training_runs
                        == NVX_NODE_RUNTIME_PROFILE_RUNS
                    ):
                        break
                if successful_training_runs != NVX_NODE_RUNTIME_PROFILE_RUNS:
                    raise RuntimeError(
                        "NVX runtime hot-profile training completed only "
                        f"{successful_training_runs}/{NVX_NODE_RUNTIME_PROFILE_RUNS} "
                        f"successful runs after {max_training_attempts} attempts"
                    )
                if reported_training_runs < NVX_NODE_RUNTIME_PROFILE_RUNS:
                    raise RuntimeError(
                        "NVX runtime hot-profile sidecar reports only "
                        f"{reported_training_runs} training runs"
                    )
                require_file(node_runtime_profile_path)
                completed_node_runtime_config = {
                    **expected_node_runtime_config,
                    "hot_profile": {
                        "sha256": sha256(node_runtime_profile_path),
                        "training_runs": reported_training_runs,
                    },
                }
                node_runtime_config_path.write_text(
                    json.dumps(completed_node_runtime_config, indent=2),
                    encoding="utf-8",
                )
            require_file(node_runtime_snapshot / "state.bin")
            require_file(node_runtime_snapshot / "mem.bin")
            require_file(node_runtime_profile_path)
            validated_node_runtime_config = json.loads(
                node_runtime_config_path.read_text(encoding="utf-8")
            )
            if not isinstance(validated_node_runtime_config, dict):
                raise RuntimeError("NVX runtime snapshot config is invalid")
            validated_profile_record = validated_node_runtime_config.get(
                "hot_profile"
            )
            if not isinstance(validated_profile_record, dict):
                raise RuntimeError(
                    "NVX runtime snapshot config is missing hot-profile provenance"
                )
            node_runtime_profile_provenance = {
                "sha256": validated_profile_record["sha256"],
                "training_runs": validated_profile_record["training_runs"],
            }
            specs[("nvx", "runtime-preinitialized")] = CommandSpec(
                "nvx",
                "runtime-preinitialized",
                executable,
                (
                    "--restore",
                    str(node_runtime_snapshot),
                    "--mem",
                    str(NVX_NODE_RUNTIME_MEMORY_MIB),
                    "--console",
                    "virtio",
                    "--output-after-marker",
                    NVX_NODE_RUNTIME_MARKER,
                    "--snapshot-prefetch",
                    "auto",
                    "--log-level",
                    "off",
                ),
                NVX_DIR,
                marker,
                request_script=runtime_sample,
                additional_success_markers=(
                    "snapshot-hot-pages: status=active",
                    "populate=ok",
                ),
            )

        snapshot_generation_arguments: list[str] = [
            "--kernel",
            str(kernel),
            "--initrd",
            str(initrd),
            "--mem",
            str(memory["nvx"]),
            "--cmdline",
            cmdline,
            "--mount",
            str(nvx_mount),
            "--snapshot",
            str(nvx_generation_snapshot),
            "--quiet",
            "--log-level",
            "info",
        ]
        if not nvx_is_python:
            snapshot_generation_arguments.extend(
                ["--exec", nvx_guest_app, "--snapshot-before-exec"]
            )

        if nvx_is_python:
            restore_arguments = (
                "--restore",
                str(nvx_snapshot),
                "--mem",
                str(memory["nvx"]),
                "--mount",
                str(nvx_mount),
                "--exit-on-boot",
                "--quiet",
                "--boot-marker",
                marker,
            )
            success_marker = "to marker"
        else:
            restore_arguments = (
                "--restore",
                str(nvx_snapshot),
                "--mem",
                str(memory["nvx"]),
                "--mount",
                str(nvx_mount),
                "--output-after-marker",
                "NVX-EXEC-START",
                "--log-level",
                "off",
            )
            success_marker = marker

        specs[("nvx", "snapshot-generation")] = CommandSpec(
            "nvx",
            "snapshot-generation",
            executable,
            tuple(snapshot_generation_arguments),
            NVX_DIR,
            "snapshot written to",
            reset_path=nvx_generation_snapshot,
            success_paths=(
                nvx_generation_snapshot / "state.bin",
                nvx_generation_snapshot / "mem.bin",
            ),
        )
        specs[("nvx", "restore")] = CommandSpec(
            "nvx",
            "restore",
            executable,
            restore_arguments,
            NVX_DIR,
            success_marker,
        )
        if nvx_is_python:
            specs[("nvx", "warm")] = CommandSpec(
                "nvx",
                "warm",
                executable,
                (
                    "--restore",
                    str(nvx_snapshot),
                    "--mem",
                    str(memory["nvx"]),
                    "--mount",
                    str(nvx_warm_mount),
                    "--log-level",
                    "off",
                ),
                NVX_DIR,
                "BENCHMARK_OK",
                nvx_warm_mount / NVX_WARM_ITERATIONS_FILE,
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
        generation_work = nanvix_work / "snapshot-generation"
        generation_work.mkdir(parents=True, exist_ok=True)
        generation_snapshot = generation_work / "snapshots"
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
            if (output_dir / "raw.csv").is_file() and "nanvix" in previous_vmms:
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

        specs[("nanvix", "snapshot-generation")] = CommandSpec(
            "nanvix",
            "snapshot-generation",
            executable,
            (
                "-bin-dir",
                str(bin_dir),
                "-ramfs",
                str(ramfs),
                "-kernel-args",
                "snapshot",
                "--",
                str(initrd),
            ),
            generation_work,
            None,
            reset_path=generation_snapshot,
            success_paths=(
                generation_snapshot / "kernel.whp.cbor",
                generation_snapshot / "kernel.vmem",
            ),
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
        generation_snapshot = hyperlight_work / "snapshot-generation"
        hyperlight_work.mkdir(parents=True, exist_ok=True)
        runner = require_file(
            HYPERLIGHT_RUNNER_DIR / "target" / "release" / "vmm-hyperlight-runner.exe"
        )

        hl_image = HYPERLIGHT_WORKLOAD_IMAGE[workload]
        hl_kernel = hyperlight_work / "kernel"
        hl_initrd = hyperlight_work / "initrd.cpio"
        is_pyhl = workload in HYPERLIGHT_PYHL_WORKLOADS
        hl_heap = memory["hyperlight"]
        hl_app_args = HYPERLIGHT_APP_ARGS.get(workload, ())

        artifact_acquisition = stage_hyperlight_artifacts(
            hl_image,
            hl_kernel,
            hl_initrd,
            use_docker=use_docker,
        )
        hyperlight_artifact_provenance = {
            "image": hl_image,
            "acquisition": artifact_acquisition,
            "kernel_sha256": sha256(hl_kernel),
            "initrd_sha256": sha256(hl_initrd),
            "runner_sha256": sha256(runner),
            "app_args": list(hl_app_args),
        }

        # Snapshot validity check
        snapshot_config_path = snapshot / "benchmark-config.json"
        expected_snapshot_config: dict[str, object] = {
            "format": HYPERLIGHT_SNAPSHOT_FORMAT,
            "image": hl_image,
            "kernel_sha256": sha256(hl_kernel),
            "initrd_sha256": sha256(hl_initrd),
            "runner_sha256": sha256(runner),
            "pyhl": is_pyhl,
            "heap_mib": hl_heap,
            "app_args": list(hl_app_args),
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
            if (output_dir / "raw.csv").is_file() and "hyperlight" in previous_vmms:
                raise RuntimeError(
                    "Hyperlight snapshot does not match the current workload image "
                    f"in {output_dir}; choose a new --output directory"
                )
            shutil.rmtree(snapshot, ignore_errors=True)
            capture_cmd: list[str | Path] = [
                runner, "capture", hl_kernel, hl_initrd, snapshot,
                "--heap-size", f"{hl_heap}Mi",
            ]
            if is_pyhl:
                capture_cmd.append("--warmup")
            if hl_app_args:
                capture_cmd.extend(["--", *hl_app_args])
            run_control(
                capture_cmd,
                cwd=HYPERLIGHT_RUNNER_DIR,
                timeout=timeout,
                marker="SNAPSHOT_OK",
            )
            snapshot_config_path.write_text(
                json.dumps(expected_snapshot_config, indent=2),
                encoding="utf-8",
            )
        require_file(snapshot / "index.json")

        generation_args: list[str] = [
            "snapshot-generation",
            str(hl_kernel),
            str(hl_initrd),
            str(generation_snapshot),
            "--heap-size",
            f"{hl_heap}Mi",
        ]
        if is_pyhl:
            generation_args.append("--warmup")
        if hl_app_args:
            generation_args.extend(["--", *hl_app_args])

        if is_pyhl:
            assert sample is not None
            restore_args = ("restore", str(hl_initrd), str(snapshot), str(sample))
        else:
            restore_args = ("restore", str(hl_initrd), str(snapshot))

        specs[("hyperlight", "snapshot-generation")] = CommandSpec(
            "hyperlight",
            "snapshot-generation",
            runner,
            tuple(generation_args),
            HYPERLIGHT_RUNNER_DIR,
            "BENCHMARK_OK",
            reset_path=generation_snapshot,
            success_paths=(generation_snapshot / "index.json",),
        )
        specs[("hyperlight", "restore")] = CommandSpec(
            "hyperlight",
            "restore",
            runner,
            restore_args,
            HYPERLIGHT_RUNNER_DIR,
            "BENCHMARK_OK",
            additional_success_markers=(
                (HYPERLIGHT_EMBEDDED_MARKERS[workload],)
                if workload in HYPERLIGHT_EMBEDDED_MARKERS
                else ()
            ),
        )

        # Warm reuse — load snapshot once, call guest repeatedly with
        # in-memory state rewind between calls.  pyhl workloads pass a
        # script to call_named; non-pyhl use call_run (no script arg).
        if is_pyhl:
            assert sample is not None
            warm_args = ("warm", str(hl_initrd), str(snapshot), str(sample))
        else:
            warm_args = ("warm", str(hl_initrd), str(snapshot))
        specs[("hyperlight", "warm")] = CommandSpec(
            "hyperlight",
            "warm",
            runner,
            warm_args,
            HYPERLIGHT_RUNNER_DIR,
            "BENCHMARK_OK",
        )

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
                **(
                    {"reset_path_before_timing": str(spec.reset_path)}
                    if spec.reset_path is not None
                    else {}
                ),
                **(
                    {
                        "required_artifacts": [
                            str(path) for path in spec.success_paths
                        ]
                    }
                    if spec.success_paths
                    else {}
                ),
                **(
                    {"iterations_file": str(spec.iterations_file)}
                    if spec.iterations_file is not None
                    else {}
                ),
                **(
                    {
                        "stdin_request": {
                            "protocol": "8-byte big-endian JSON length + JSON",
                            "mode": NVX_NODE_RUNTIME_REQUEST_MODE,
                            "script": str(spec.request_script),
                            "fresh_per_sample": ["entropy", "unixTimeNs"],
                        }
                    }
                    if spec.request_script is not None
                    else {}
                ),
                **(
                    {
                        "additional_success_markers": list(
                            spec.additional_success_markers
                        )
                    }
                    if spec.additional_success_markers
                    else {}
                ),
            }
            for (vmm, mode), spec in specs.items()
        }
    )
    manifest_vmms = {
        key.partition("/")[0]
        for key in commands
        if key.partition("/")[0] in VMM_ORDER
    }
    delivery: dict[str, dict[str, str]] = {
        "nanvix": {"method": "host_mount", "guest_path": "/mnt/hello.py"},
    }
    if workload in NVX_PYTHON_WORKLOADS:
        delivery["nvx"] = {"method": "host_mount", "guest_path": "app.py"}
    elif workload in NVX_MOUNTED_EXEC_WORKLOADS:
        delivery["nvx"] = {
            "method": "mounted_exec",
            "guest_path": "/mnt/host/app.sh",
        }
    if workload in HYPERLIGHT_PYHL_WORKLOADS:
        delivery["hyperlight"] = {"method": "function_call", "function": "run"}
    else:
        delivery["hyperlight"] = {"method": "embedded_in_initrd"}
    previous_snapshot_policy_value = previous_manifest.get("snapshot_policy")
    previous_snapshot_policy = (
        cast(dict[str, object], previous_snapshot_policy_value)
        if isinstance(previous_snapshot_policy_value, dict)
        else {}
    )
    previous_build_patches_value = previous_snapshot_policy.get("build_patches")
    previous_build_patches = (
        cast(dict[str, object], previous_build_patches_value)
        if isinstance(previous_build_patches_value, dict)
        else {}
    )
    build_patches: dict[str, object] = {
        vmm: previous_build_patches[vmm]
        for vmm in manifest_vmms - selected
        if vmm in previous_build_patches
    }
    if "nvx" in selected:
        build_patches["nvx"] = {
            "path": str(NVX_BUILD_PATCH.relative_to(SCRIPT_DIR)),
            "sha256": sha256(NVX_BUILD_PATCH),
        }
    if "nanvix" in selected:
        build_patches["nanvix"] = {
            "path": str(NANVIX_BUILD_PATCH.relative_to(SCRIPT_DIR)),
            "sha256": sha256(NANVIX_BUILD_PATCH),
        }
    missing_build_patches = (
        manifest_vmms.intersection({"nvx", "nanvix"}) - set(build_patches)
    )
    if missing_build_patches:
        missing = ", ".join(sorted(missing_build_patches))
        raise RuntimeError(f"manifest is missing build-patch provenance for: {missing}")
    previous_capture_points_value = previous_snapshot_policy.get("capture_points")
    previous_capture_points = (
        cast(dict[str, object], previous_capture_points_value)
        if isinstance(previous_capture_points_value, dict)
        else {}
    )
    capture_points: dict[str, str] = {
        vmm: value
        for vmm in manifest_vmms - selected
        if isinstance((value := previous_capture_points.get(vmm)), str)
    }
    if "nvx" in selected:
        if workload == "pandoc-docx":
            capture_points["nvx"] = (
                "initialized_cpython_with_numpy_pandas_pypandoc_before_host_mount"
            )
        elif workload in NVX_PYTHON_WORKLOADS:
            capture_points["nvx"] = "initialized_untrained_cpython_before_host_mount"
        else:
            capture_points["nvx"] = "initialized_linux_before_mounted_exec"
    if "nanvix" in selected:
        capture_points["nanvix"] = "initialized_cpython_and_ramfs_before_host_mount"
    if "hyperlight" in selected:
        capture_points["hyperlight"] = (
            "initialized_cpython_driver"
            if workload in HYPERLIGHT_PYHL_WORKLOADS
            else "initialized_unikraft_elfloader"
        )
    missing_capture_points = manifest_vmms - capture_points.keys()
    if missing_capture_points:
        missing = ", ".join(sorted(missing_capture_points))
        raise RuntimeError(f"manifest is missing capture provenance for: {missing}")
    previous_runtime_policy_value = previous_snapshot_policy.get(
        "runtime_preinitialized"
    )
    runtime_policy = (
        cast(dict[str, object], previous_runtime_policy_value).copy()
        if isinstance(previous_runtime_policy_value, dict)
        else {}
    )
    if "nvx" in selected:
        if workload == "nodejs-hello":
            assert node_runtime_profile_provenance is not None
            runtime_policy["nvx"] = {
                "mode": NVX_NODE_RUNTIME_REQUEST_MODE,
                "capture_point": (
                    "initialized_nodejs_v8_worker_before_host_request"
                ),
                "workload_in_snapshot": False,
                "request_refresh": ["entropy", "realtime", "v8_random_seed"],
                "hot_profile_training_runs": NVX_NODE_RUNTIME_PROFILE_RUNS,
                "hot_profile_marker": NVX_NODE_RUNTIME_PROFILE_MARKER,
                "hot_profile": node_runtime_profile_provenance,
            }
        else:
            runtime_policy.pop("nvx", None)

    previous_sample_value = previous_manifest.get("sample")
    previous_sample = (
        cast(dict[str, object], previous_sample_value)
        if isinstance(previous_sample_value, dict)
        else {}
    )
    previous_sample_by_vmm_value = previous_sample.get("by_vmm")
    previous_sample_by_vmm = (
        cast(dict[str, object], previous_sample_by_vmm_value)
        if isinstance(previous_sample_by_vmm_value, dict)
        else {}
    )
    sample_by_vmm: dict[str, object] = {}
    for vmm in manifest_vmms:
        if vmm not in selected:
            previous_record = previous_sample_by_vmm.get(vmm)
            if not isinstance(previous_record, dict):
                raise RuntimeError(
                    f"manifest is missing sample provenance for retained target {vmm}"
                )
            sample_by_vmm[vmm] = previous_record
            continue

        if vmm in WORKLOAD_SAMPLE_VMMS[workload]:
            assert sample is not None and workload_sample_source is not None
            record: dict[str, object] = {
                "source": str(workload_sample_source.relative_to(SCRIPT_DIR)),
                "sha256": sha256(sample),
                "delivery": delivery[vmm],
            }
        else:
            record = {
                "source": "embedded_in_initrd",
                "delivery": delivery[vmm],
            }
        if vmm == "hyperlight":
            assert hyperlight_artifact_provenance is not None
            record["runtime_artifact"] = hyperlight_artifact_provenance
        if vmm == "nvx" and workload == "nodejs-hello":
            assert runtime_sample is not None
            record["mode_sources"] = {
                "runtime-preinitialized": {
                    "source": str(NODEJS_RUNTIME_SAMPLE.relative_to(SCRIPT_DIR)),
                    "sha256": sha256(runtime_sample),
                    "delivery": {
                        "method": "virtio_console_request",
                        "mode": NVX_NODE_RUNTIME_REQUEST_MODE,
                    },
                }
            }
        sample_by_vmm[vmm] = record
    sample_record: dict[str, object] = {"by_vmm": sample_by_vmm}
    dependencies: dict[str, dict[str, str]] = {}
    if workload in {"pandoc-docx", "pandoc-native"}:
        dependencies["pandoc"] = {
            "distribution": "Alpine 3.24",
            "version": "3.10",
        }
    if workload == "pandoc-docx":
        dependencies["pypandoc"] = {
            "version": PYPANDOC_VERSION,
            "wheel_sha256": PYPANDOC_WHEEL_SHA256,
        }
    if workload == "nodejs-hello":
        dependencies["nodejs"] = {"distribution": "Alpine 3.24"}
    previous_warmup_by_vmm_value = previous_snapshot_policy.get("warmup_by_vmm")
    previous_warmup_by_vmm = (
        cast(dict[str, object], previous_warmup_by_vmm_value)
        if isinstance(previous_warmup_by_vmm_value, dict)
        else {}
    )
    warmup_by_vmm: dict[str, object] = {
        vmm: previous_warmup_by_vmm[vmm]
        for vmm in manifest_vmms - selected
        if vmm in previous_warmup_by_vmm
    }
    if "nvx" in selected:
        warmup_by_vmm["nvx"] = (
            {
                "statement": GENERIC_SNAPSHOT_WARMUP,
                "imports": (
                    list(NVX_WARMUP_IMPORTS)
                    if workload == "pandoc-docx"
                    else []
                ),
            }
            if workload in NVX_PYTHON_WORKLOADS
            else None
        )
    if "nanvix" in selected:
        warmup_by_vmm["nanvix"] = GENERIC_SNAPSHOT_WARMUP
    if "hyperlight" in selected:
        warmup_by_vmm["hyperlight"] = (
            GENERIC_SNAPSHOT_WARMUP
            if workload in HYPERLIGHT_PYHL_WORKLOADS
            else None
        )
    missing_warmups = manifest_vmms - warmup_by_vmm.keys()
    if missing_warmups:
        missing = ", ".join(sorted(missing_warmups))
        raise RuntimeError(f"manifest is missing snapshot warmup provenance for: {missing}")

    previous_memory_value = previous_manifest.get("guest_memory_mib")
    previous_memory = (
        cast(dict[str, object], previous_memory_value)
        if isinstance(previous_memory_value, dict)
        else {}
    )
    manifest_memory: dict[str, int] = {}
    for vmm in manifest_vmms:
        if vmm in selected:
            manifest_memory[vmm] = memory[vmm]
            continue
        previous_mib = previous_memory.get(vmm)
        if not isinstance(previous_mib, int):
            raise RuntimeError(
                f"manifest is missing guest memory provenance for retained target {vmm}"
            )
        manifest_memory[vmm] = previous_mib
    previous_mode_memory_value = previous_manifest.get("guest_memory_mib_by_mode")
    previous_mode_memory = (
        cast(dict[str, object], previous_mode_memory_value)
        if isinstance(previous_mode_memory_value, dict)
        else {}
    )
    manifest_mode_memory: dict[str, dict[str, int]] = {}
    for vmm in manifest_vmms:
        if vmm in selected:
            manifest_mode_memory[vmm] = {
                mode: (
                    NVX_NODE_RUNTIME_MEMORY_MIB
                    if vmm == "nvx" and mode == "runtime-preinitialized"
                    else memory[vmm]
                )
                for target, mode in specs
                if target == vmm
            }
            continue
        retained = previous_mode_memory.get(vmm)
        if not isinstance(retained, dict) or not all(
            isinstance(mode, str) and isinstance(mib, int)
            for mode, mib in retained.items()
        ):
            raise RuntimeError(
                f"manifest is missing per-mode memory provenance for {vmm}"
            )
        manifest_mode_memory[vmm] = cast(dict[str, int], retained)

    if "nvx" in selected:
        nvx_import_warmup: object = (
            list(NVX_WARMUP_IMPORTS) if workload == "pandoc-docx" else []
        )
        nvx_warm_reuse: object = (
            {
                "isolation": "forked_child",
                "iterations_file": NVX_WARM_ITERATIONS_FILE,
            }
            if workload in NVX_PYTHON_WORKLOADS
            else None
        )
    elif "nvx" in manifest_vmms:
        nvx_import_warmup = previous_snapshot_policy.get("nvx_import_warmup", [])
        nvx_warm_reuse = previous_snapshot_policy.get("nvx_warm_reuse")
    else:
        nvx_import_warmup = []
        nvx_warm_reuse = None

    if "hyperlight" in selected:
        hyperlight_workload_image: object = HYPERLIGHT_WORKLOAD_IMAGE[workload]
    elif "hyperlight" in manifest_vmms:
        hyperlight_workload_image = previous_snapshot_policy.get(
            "hyperlight_workload_image", ""
        )
    else:
        hyperlight_workload_image = ""

    manifest = {
        "format": MANIFEST_FORMAT,
        "harness": {
            "benchmark_sha256": sha256(Path(__file__)),
            "repository_commit": git_head(SCRIPT_DIR),
        },
        "created_at_utc": previous_manifest.get("created_at_utc", utc_now()),
        "updated_at_utc": utc_now(),
        "sample": sample_record,
        "workload": {
            "id": workload,
            "label": WORKLOAD_LABELS[workload],
            "title": WORKLOAD_TITLES[workload],
            "dependencies": dependencies,
        },
        "snapshot_policy": {
            "generic_warmup": GENERIC_SNAPSHOT_WARMUP,
            "warmup_by_vmm": warmup_by_vmm,
            "nvx_import_warmup": nvx_import_warmup,
            "nvx_warm_reuse": nvx_warm_reuse,
            "workload_in_snapshot": False,
            "build_patches": build_patches,
            "hyperlight_workload_image": hyperlight_workload_image,
            "capture_points": capture_points,
            "runtime_preinitialized": runtime_policy,
            "snapshot_generation": {
                "scope": (
                    "fresh process construction, snapshot warmup, in-memory capture, "
                    "and persistence"
                ),
                "samples_per_vmm_workload": SNAPSHOT_GENERATION_SAMPLES,
                "artifact": "dedicated scratch snapshot, separate from the restore snapshot",
                "cleanup": (
                    "scratch snapshot removed before the timed process starts and after "
                    "required artifacts are validated"
                ),
            },
        },
        "guest_memory_mib": manifest_memory,
        "guest_memory_mib_by_mode": manifest_mode_memory,
        "workloads": {
            vmm: WORKLOAD_LABELS[workload]
            for vmm in manifest_vmms
        },
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


def write_metadata(
    output_dir: Path,
    specs: dict[tuple[str, str], CommandSpec],
    workload: str,
    *,
    use_docker: bool = False,
) -> None:
    active_vmms = {spec.vmm for spec in specs.values()}
    included_vmms = set(active_vmms)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        loaded_manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded_manifest, dict):
            manifest = cast(dict[str, object], loaded_manifest)
            manifest_commands = manifest.get("commands")
            if isinstance(manifest_commands, dict):
                included_vmms.update(
                    key.partition("/")[0]
                    for key in manifest_commands
                    if isinstance(key, str) and key.partition("/")[0] in VMM_ORDER
                )
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
    workload_sample = WORKLOAD_SAMPLES[workload]
    supported_source_paths: set[Path] = (
        {workload_sample}
        if (
            workload_sample is not None
            and included_vmms.intersection(WORKLOAD_SAMPLE_VMMS[workload])
        )
        else set()
    )
    refreshed_source_paths: set[Path] = (
        {workload_sample}
        if (
            workload_sample is not None
            and active_vmms.intersection(WORKLOAD_SAMPLE_VMMS[workload])
        )
        else set()
    )
    if workload == "nodejs-hello" and "nvx" in included_vmms:
        supported_source_paths.add(NODEJS_RUNTIME_SAMPLE)
    if workload == "nodejs-hello" and "nvx" in active_vmms:
        refreshed_source_paths.add(NODEJS_RUNTIME_SAMPLE)
    if "nvx" in included_vmms:
        supported_source_paths.update(
            {
                NVX_DIR / "target" / "release" / "microvm.exe",
                NVX_DIR / "build" / "vmlinux",
                NVX_PYTHON_INITRD,
                NVX_NODE_RUNTIME_INITRD,
            }
        )
    if "nvx" in active_vmms:
        refreshed_source_paths.update(
            {
                NVX_DIR / "target" / "release" / "microvm.exe",
                NVX_DIR / "build" / "vmlinux",
                NVX_PYTHON_INITRD,
                NVX_NODE_RUNTIME_INITRD,
            }
        )
    if "hyperlight" in included_vmms:
        supported_source_paths.add(
            HYPERLIGHT_RUNNER_DIR
            / "target"
            / "release"
            / "vmm-hyperlight-runner.exe"
        )
    if "hyperlight" in active_vmms:
        refreshed_source_paths.add(
            HYPERLIGHT_RUNNER_DIR
            / "target"
            / "release"
            / "vmm-hyperlight-runner.exe"
        )
    artifact_paths.update(path for path in refreshed_source_paths if path.is_file())
    if workload == "nodejs-hello" and "nvx" in included_vmms:
        runtime_profile = (
            output_dir
            / "artifacts"
            / "nvx"
            / "runtime-preinitialized-snapshot"
            / "hot-pages.whp.v1"
        )
        if runtime_profile.is_file():
            artifact_paths.add(runtime_profile)
    metadata_path = output_dir / "metadata.json"
    previous_metadata: dict[str, object] = {}
    if metadata_path.is_file():
        loaded_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(loaded_metadata, dict):
            previous_metadata = loaded_metadata
    previous_artifacts = previous_metadata.get("artifacts", [])
    supported_source_artifacts = {path.resolve() for path in supported_source_paths}
    output_artifacts = (output_dir / "artifacts").resolve()

    def is_supported_artifact(path: Path) -> bool:
        resolved = path.resolve()
        if resolved in supported_source_artifacts:
            return True
        if not path.is_file():
            return False
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
            "docker": (
                version_output(["docker", "--version"])
                if use_docker
                else "disabled"
            ),
        },
        "configuration": {"use_docker": use_docker},
        "repositories": {
            "vmm-benchmarks": git_metadata(SCRIPT_DIR),
            "nvx": git_metadata(NVX_DIR),
            "nanvix-python": git_metadata(NANVIX_DIR),
            "hyperlight-unikraft": git_metadata(HYPERLIGHT_DIR),
        },
        "artifacts": [
            artifacts_by_path[path] for path in sorted(artifacts_by_path)
        ],
        "workload": {
            "id": workload,
            "label": WORKLOAD_LABELS[workload],
        },
        "measurement": {
            "elapsed": "perf_counter_ns around direct VMM process creation through exit",
            "peak_rss": (
                "Windows GetProcessMemoryInfo PeakWorkingSetSize after process exit; "
                "warm mode records one observation per multi-iteration process"
            ),
            "hyperlight_surrogates": "disabled with configure_surrogates(Some(0))",
            "hyperlight_workload": "host script source passed to initialized CPython function run",
            "hyperlight_phases": (
                "runner-reported sandbox build, initial rewind, snapshot capture and "
                "persistence, persisted snapshot load, and guest call"
            ),
            "snapshot_generation": (
                "dedicated scratch snapshot removed before timing; timed process includes "
                "VM construction, warmup, capture, persistence, and process teardown"
            ),
            "nvx_runtime_preinitialized": (
                "separately labeled initialized-V8 snapshot; each timed restore receives "
                "fresh entropy, realtime, V8 random seed, and JavaScript over virtio-console"
                if (
                    "nvx" in included_vmms
                    and workload == "nodejs-hello"
                )
                else "not used"
            ),
            "sample_counts": {
                "snapshot_generation_per_vmm_workload": SNAPSHOT_GENERATION_SAMPLES,
                "restore_runtime_preinitialized_and_warm": (
                    "configured by --samples"
                ),
            },
            "nvx_warm_reuse": (
                "one restored VM; each invocation runs in a forked child of the "
                "snapshot-initialized CPython process"
                if "nvx" in included_vmms and workload in NVX_PYTHON_WORKLOADS
                else "not used"
            ),
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
    missing_marker = (
        spec.success_marker is not None
        and spec.success_marker not in result.output
    )
    missing_additional = [
        marker
        for marker in spec.additional_success_markers
        if marker not in result.output
    ]
    if result.exit_code != 0 or missing_marker or missing_additional:
        raise RuntimeError(
            f"{spec.vmm}/{spec.mode} failed: exit={result.exit_code}, "
            f"missing_marker={missing_marker}, "
            f"missing_additional_markers={missing_additional}\n{result.output}"
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
        for sample in range(
            1,
            (
                SNAPSHOT_GENERATION_SAMPLES
                if mode == "snapshot-generation"
                else samples
            )
            + 1,
        )
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
                f"[{index:03d}/{total:03d}] {vmm:10s} {mode:19s} "
                f"sample={sample:03d} time={result.elapsed_ms:9.3f} ms "
                f"peak-rss={result.peak_rss_bytes / (1024 * 1024):8.2f} MiB",
                flush=True,
            )
            if cooldown_ms:
                time.sleep(cooldown_ms / 1000.0)


def warm_command(spec: CommandSpec, iterations: int) -> list[str]:
    if spec.iterations_file is None:
        return [*spec.command, "--iterations", str(iterations)]
    spec.iterations_file.parent.mkdir(parents=True, exist_ok=True)
    spec.iterations_file.write_text(str(iterations), encoding="ascii")
    return spec.command


def run_warm_benchmark(
    output_dir: Path,
    spec: CommandSpec,
    *,
    samples: int,
    timeout: float,
    preflight: bool,
) -> None:
    """Run a single warm-reuse process with N iterations and record each as a sample.

    Unlike snapshot generation and restore (one process per sample), warm reuse
    loads the snapshot once and invokes the guest repeatedly with runtime-specific
    state isolation. Each iteration's guest call plus isolation/reset time is
    recorded as elapsed_ms.
    """
    raw_path = output_dir / "raw.csv"
    failures = output_dir / "failures"
    failures.mkdir(parents=True, exist_ok=True)
    completed = load_completed(raw_path)
    warm_completed = {s for vmm, mode, s in completed if vmm == spec.vmm and mode == "warm"}
    missing_samples = [
        sample for sample in range(1, samples + 1) if sample not in warm_completed
    ]
    if not missing_samples:
        return

    if preflight and not warm_completed:
        print("Running one unrecorded warm preflight iteration...", flush=True)
        preflight_cmd = warm_command(spec, 1)
        run_control(
            preflight_cmd,
            cwd=spec.cwd,
            timeout=timeout,
            marker="BENCHMARK_OK",
        )

    command = warm_command(spec, len(missing_samples))
    rendered = [str(item) for item in command]
    print(f"+ ({spec.cwd}) {command_text(rendered)}", flush=True)
    process = subprocess.Popen(
        rendered,
        cwd=spec.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output_bytes, _ = process.communicate(timeout=timeout * len(missing_samples))
    except subprocess.TimeoutExpired:
        process.kill()
        output_bytes, _ = process.communicate()
        raise RuntimeError(
            f"warm benchmark timed out after "
            f"{timeout * len(missing_samples):.1f}s\n"
            f"{output_bytes.decode('utf-8', errors='replace')}"
        )
    peak_rss = peak_working_set(process)
    output = output_bytes.decode("utf-8", errors="replace")

    missing_marker = (
        spec.success_marker is not None
        and spec.success_marker not in output
    )
    if process.returncode != 0 or missing_marker:
        failure_path = failures / f"{spec.vmm}-warm.log"
        failure_path.write_text(
            f"exit={process.returncode}\n{output}", encoding="utf-8"
        )
        raise RuntimeError(f"warm benchmark failed; details: {failure_path}")

    # Parse per-iteration timings from BENCHMARK_PHASE lines
    iterations: list[dict[str, float]] = []
    for line in output.splitlines():
        if not line.startswith("BENCHMARK_PHASE "):
            continue
        fields: dict[str, float] = {}
        for field in line.removeprefix("BENCHMARK_PHASE ").split():
            name, separator, value = field.partition("=")
            if separator:
                fields[name] = float(value)
        if "warm_iteration" in fields:
            iterations.append(fields)

    if len(iterations) != len(missing_samples):
        raise RuntimeError(
            f"expected {len(missing_samples)} warm iterations, got {len(iterations)}"
        )

    write_header = not raw_path.is_file()
    sequence = len(completed)
    with raw_path.open("a", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=RAW_FIELDS)
        if write_header:
            writer.writeheader()
            destination.flush()
            os.fsync(destination.fileno())

        for iteration_index, (sample, iter_data) in enumerate(
            zip(missing_samples, iterations)
        ):
            guest_call_ms = iter_data.get("guest_call_ms", 0.0)
            rewind_ms = iter_data.get("rewind_ms", 0.0)
            elapsed_ms = guest_call_ms + rewind_ms
            rss_observation = peak_rss if iteration_index == 0 else None

            sequence += 1
            row = {
                "sequence": sequence,
                "timestamp_utc": utc_now(),
                "vmm": spec.vmm,
                "mode": "warm",
                "sample": sample,
                "elapsed_ms": f"{elapsed_ms:.6f}",
                "peak_rss_bytes": (
                    rss_observation if rss_observation is not None else ""
                ),
                "peak_rss_mib": (
                    f"{rss_observation / (1024 * 1024):.6f}"
                    if rss_observation is not None
                    else ""
                ),
                "exit_code": process.returncode,
                "guest_call_ms": f"{guest_call_ms:.6f}",
                "rewind_ms": f"{rewind_ms:.6f}",
                **{
                    field: ""
                    for field in PHASE_FIELDS
                    if field not in ("guest_call_ms", "rewind_ms")
                },
            }
            writer.writerow(row)
            destination.flush()
            os.fsync(destination.fileno())
            print(
                f"[warm {sample:03d}/{samples:03d}] {spec.vmm:10s} warm    "
                f"call={guest_call_ms:9.3f} ms rewind={rewind_ms:9.3f} ms "
                f"peak-rss={peak_rss / (1024 * 1024):8.2f} MiB",
                flush=True,
            )


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


def load_groups(
    raw_path: Path,
    mode_order: Sequence[str] = MODE_ORDER,
) -> dict[tuple[str, str], dict[str, list[float]]]:
    groups: dict[tuple[str, str], dict[str, list[float]]] = {}
    with raw_path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            vmm = row["vmm"]
            mode = row["mode"]
            if vmm not in VMM_ORDER or mode not in mode_order:
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
            if row["peak_rss_mib"]:
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
    manifest = cast(
        dict[str, object],
        json.loads((output_dir / "manifest.json").read_text(encoding="utf-8")),
    )
    workload = manifest_workload(manifest)
    workload_label = WORKLOAD_LABELS[workload]
    workload_title = WORKLOAD_TITLES[workload]
    workload_vmms = WORKLOAD_VMMS[workload]
    mode_order = manifest_mode_order(manifest)
    groups = load_groups(raw_path, mode_order)
    if not groups:
        raise RuntimeError(f"no benchmark records in {raw_path}")
    manifest_commands = manifest.get("commands")
    expected_groups = (
        {
            (vmm, mode)
            for key in manifest_commands
            if isinstance(key, str)
            for vmm, separator, mode in (key.partition("/"),)
            if (
                separator
                and vmm in VMM_ORDER
                and mode in mode_order
            )
        }
        if isinstance(manifest_commands, dict)
        else set()
    )
    missing_groups = expected_groups - groups.keys()
    if missing_groups:
        rendered = ", ".join(
            f"{vmm}/{mode}"
            for vmm, mode in sorted(
                missing_groups,
                key=lambda key: (
                    VMM_ORDER.index(key[0]),
                    mode_order.index(key[1]),
                ),
            )
        )
        raise RuntimeError(f"raw.csv is missing configured group(s): {rendered}")
    current_plot_stems = {
        f"{kind}_{workload}_{mode}"
        for kind in ("cdf_execution_time", "barplot_peak_rss")
        for mode in mode_order
    }
    for pattern in ("cdf_execution_time_*.*", "barplot_peak_rss_*.*"):
        for plot in output_dir.glob(pattern):
            if plot.stem not in current_plot_stems:
                plot.unlink()
    summaries: list[dict[str, object]] = []
    phase_summaries: list[dict[str, object]] = []
    for vmm in VMM_ORDER:
        for mode in mode_order:
            key = (vmm, mode)
            if key not in groups:
                continue
            elapsed = describe(groups[key]["elapsed_ms"])
            rss = describe(groups[key]["peak_rss_mib"])
            expected_group_samples = (
                (
                    configured_snapshot_generation_samples(manifest)
                    or expected_samples
                )
                if mode == "snapshot-generation"
                else expected_samples
            )
            if (
                expected_group_samples is not None
                and elapsed["count"] != expected_group_samples
            ):
                raise RuntimeError(
                    f"{vmm}/{mode} has {elapsed['count']} samples; "
                    f"expected {expected_group_samples}"
                )
            summaries.append(
                {
                    "vmm": vmm,
                    "workload": workload_label,
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
        "rss_count",
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
                    "rss_count": rss["count"],
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

    for mode in mode_order:
        if not any((vmm, mode) in groups for vmm in workload_vmms):
            continue
        execution_plot = output_dir / f"cdf_execution_time_{workload}_{mode}.svg"
        write_cdf_svg(
            execution_plot,
            groups,
            vmms=workload_vmms,
            metric="elapsed_ms",
            mode=mode,
            unit="Execution Time (ms)",
            title=f"{workload_title} {MODE_LABELS[mode]} Execution Time CDF",
        )
        render_svg_as_png(execution_plot)
        rss_plot = output_dir / f"barplot_peak_rss_{workload}_{mode}.svg"
        write_rss_barplot_svg(
            rss_plot,
            groups,
            vmms=tuple(vmm for vmm in RSS_VMM_ORDER if vmm in workload_vmms),
            mode=mode,
            title=f"{workload_title} {MODE_LABELS[mode]} P99 Peak Resident Memory",
        )
        render_svg_as_png(rss_plot)

    for mode in mode_order:
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
    manifest = cast(
        dict[str, object],
        json.loads((output_dir / "manifest.json").read_text(encoding="utf-8")),
    )
    metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    memory = manifest["guest_memory_mib"]
    mode_memory_value = manifest.get("guest_memory_mib_by_mode")
    mode_memory = (
        cast(dict[str, object], mode_memory_value)
        if isinstance(mode_memory_value, dict)
        else {}
    )

    def guest_memory(vmm: str, mode: str) -> int:
        by_mode = mode_memory.get(vmm)
        if isinstance(by_mode, dict):
            value = cast(dict[str, object], by_mode).get(mode)
            if isinstance(value, int):
                return value
        fallback = cast(dict[str, object], memory).get(vmm)
        if not isinstance(fallback, int):
            raise RuntimeError(f"manifest is missing guest memory for {vmm}/{mode}")
        return fallback

    workload = manifest_workload(manifest)
    mode_order = manifest_mode_order(manifest)
    workload_title = WORKLOAD_TITLES[workload]
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
    if (
        snapshot_policy is not None
        and snapshot_policy.get("workload_in_snapshot") is False
    ):
        snapshot_method_lines = []
        warmup_by_vmm = snapshot_policy.get("warmup_by_vmm")
        if isinstance(warmup_by_vmm, dict):
            for vmm in VMM_ORDER:
                warmup = cast(dict[str, object], warmup_by_vmm).get(vmm)
                if vmm not in included_vmms or warmup is None:
                    continue
                snapshot_method_lines.append(
                    f"- {VMM_LABELS[vmm]} executes the generic Python statement "
                    f"`{GENERIC_SNAPSHOT_WARMUP}` before snapshot capture."
                )
        elif snapshot_policy.get("generic_warmup") == GENERIC_SNAPSHOT_WARMUP:
            snapshot_method_lines.append(
                "- Every snapshot executes the generic Python statement `pass` before capture."
            )
        nvx_import_warmup = snapshot_policy.get("nvx_import_warmup")
        if "nvx" in included_vmms and isinstance(nvx_import_warmup, list):
            warmup_names = cast(list[object], nvx_import_warmup)
            imports = ", ".join(
                name for name in warmup_names if isinstance(name, str)
            )
            if imports:
                snapshot_method_lines.append(
                    f"- NVX warm-imports {imports} before the persisted snapshot is captured."
                )
        if "nvx" in included_vmms:
            if workload in NVX_PYTHON_WORKLOADS:
                snapshot_method_lines.append(
                    "- NVX mounts the selected Python source only after snapshot resume."
                )
            else:
                snapshot_method_lines.append(
                    "- NVX snapshots initialized Linux immediately before mounting and "
                    "executing the selected native workload."
                )
            if (
                workload == "nodejs-hello"
                and ("nvx", "runtime-preinitialized") in {
                    (str(summary["vmm"]), str(summary["mode"]))
                    for summary in summaries
                }
            ):
                snapshot_method_lines.append(
                    "- NVX runtime-preinitialized mode snapshots an initialized V8 "
                    "worker before receiving JavaScript, then refreshes entropy, realtime, "
                    "and V8 random state on every restore."
                )
        if "hyperlight" in included_vmms:
            if workload in HYPERLIGHT_PYHL_WORKLOADS:
                snapshot_method_lines.append(
                    "- Hyperlight snapshots an initialized CPython driver, then passes the "
                    "sample source to its `run` function after restore."
                )
            else:
                snapshot_method_lines.append(
                    "- Hyperlight snapshots its initialized workload image and invokes the "
                    "image entry point after restore."
                )
    else:
        snapshot_method_lines = [
            "- This result predates explicit snapshot-policy metadata; generic warmup and "
            "workload-independent capture cannot be inferred from these files."
        ]
    memory_text = "; ".join(
        f"{vmm}: "
        + ", ".join(
            f"{mode} {guest_memory(vmm, mode)} MiB"
            for mode in mode_order
            if any(
                str(summary["vmm"]) == vmm
                and str(summary["mode"]) == mode
                for summary in summaries
            )
        )
        for vmm in VMM_ORDER
        if vmm in included_vmms
    )
    summary_by_key = {
        (str(summary["vmm"]), str(summary["mode"])): summary for summary in summaries
    }
    generation_counts = {
        cast(dict[str, object], summary["execution_time_ms"])["count"]
        for summary in summaries
        if str(summary["mode"]) == "snapshot-generation"
        and isinstance(summary["execution_time_ms"], dict)
    }
    generation_count_text = (
        "/".join(str(count) for count in sorted(generation_counts))
        if generation_counts
        else ""
    )
    lifecycle_subject = ", ".join(
        mode
        for mode in mode_order
        if mode != "warm"
        and any(str(summary["mode"]) == mode for summary in summaries)
    )
    lines = [
        f"# VMM {workload_title} benchmark",
        "",
        "End-to-end process lifecycle and runner-internal phases are reported separately. "
        f"Every {lifecycle_subject} sample starts a new host process. Host filesystem "
        "caches were warmed by one unrecorded preflight and were not dropped.",
        "",
        "## End-to-end process lifecycle",
    ]
    cold_description = (
        "A fresh process constructs a sandbox from kernel/initrd and runs the workload; "
        "no persisted VM snapshot is loaded."
    )
    if "hyperlight" in included_vmms:
        cold_description += (
            " In MXC's deployment model the snapshot is configured at install "
            "time, so Hyperlight always boots from a pre-warmed persisted "
            "snapshot — cold and restore are the same operation."
        )
    warm_details = []
    if "hyperlight" in included_vmms:
        warm_details.append(
            "Hyperlight rewinds the in-memory sandbox between calls."
        )
    if "nvx" in included_vmms and workload in NVX_PYTHON_WORKLOADS:
        warm_details.append(
            "NVX keeps the restored CPython parent alive and forks an isolated child "
            "for each call."
        )
    mode_descriptions = {
        "snapshot-generation": (
            "A fresh process constructs a VM or sandbox from kernel/initrd, performs the "
            "configured workload-independent warmup, captures VM state, persists a dedicated "
            "scratch snapshot, and exits. The harness removes that scratch artifact before "
            "starting each timer and again after validating the generated files; it is "
            "separate from the reusable restore snapshot."
        ),
        "cold": cold_description,
        "restore": (
            "A fresh process loads one reusable persisted snapshot, invokes the workload, "
            "and exits."
        ),
        "runtime-preinitialized": (
            "A fresh NVX process restores a separately labeled snapshot containing an "
            "initialized V8 worker. The JavaScript source arrives only after restore with "
            "fresh entropy and realtime; this capture point excludes runtime initialization "
            "and is not an ordinary restore result."
        ),
        "warm": (
            "A single process loads one persisted snapshot, then invokes the workload "
            "repeatedly with state isolation between calls. Each sample includes the "
            "guest call plus its isolation/reset operation; the snapshot is loaded only "
            "once and is not timed. "
            + " ".join(warm_details)
        ),
    }
    for mode in mode_order:
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
                "RSS n | Median peak RSS (MiB) | p95 peak RSS (MiB) | "
                "Max peak RSS (MiB) |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
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
                f"{guest_memory(vmm, mode)} | {elapsed['p50']:.3f} | "
                f"{elapsed['p95']:.3f} | {rss['count']} | "
                f"{rss['p50']:.2f} | {rss['p95']:.2f} | {rss['max']:.2f} |"
            )
    if "snapshot-generation" in mode_order:
        lines.extend(
            [
                "",
                "## Snapshot generation vs resume",
                "",
                "| Target | Workload | Median generation (ms) | Median resume (ms) | "
                "Generation / resume |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for vmm in VMM_ORDER:
            generation = summary_by_key.get((vmm, "snapshot-generation"))
            restore = summary_by_key.get((vmm, "restore"))
            if generation is None or restore is None:
                continue
            generation_time = generation["execution_time_ms"]
            restore_time = restore["execution_time_ms"]
            assert isinstance(generation_time, dict) and isinstance(restore_time, dict)
            ratio = generation_time["p50"] / restore_time["p50"]
            lines.append(
                f"| {VMM_LABELS[vmm]} | {generation['workload']} | "
                f"{generation_time['p50']:.3f} | {restore_time['p50']:.3f} | "
                f"{ratio:.2f}x |"
            )
    else:
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
                f"| {VMM_LABELS[vmm]} | {cold['workload']} | {speedup:.2f}x | "
                f"{time_reduction:.1f}% | {rss_reduction:.1f}% |"
            )
    if phase_summaries:
        lines.extend(
            [
                "",
                "## Runner-internal phases",
                "",
                "These timers are emitted inside the runtime runners and are not substitutes "
                "for end-to-end latency. Phase percentiles are computed independently, so their "
                "medians do not necessarily sum to the end-to-end median.",
            ]
        )
        for mode in mode_order:
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
                "The guest-invocation phase is snapshot warmup during snapshot generation "
                "and workload execution during resume. On Hyperlight it may also include "
                "runtime initialization. Remaining "
                "process lifecycle is the end-to-end duration minus emitted internal phases and "
                "covers process startup, argument and script handling, output, and teardown.",
                "",
                "Warm reuse measures one complete steady-state call/isolation cycle per "
                "iteration. The snapshot load happens once at process start and is "
                "excluded from per-iteration timing. Peak RSS is one process-level "
                "observation per warm batch, not one independent observation per call.",
            ]
        )
    available_modes = [
        mode
        for mode in mode_order
        if any((vmm, mode) in summary_by_key for vmm in WORKLOAD_VMMS[workload])
    ]
    if available_modes:
        lines.extend(["", f"## {workload_title} plots"])
    for mode in available_modes:
        mode_label = MODE_LABELS[mode]
        lines.extend(
            [
                "",
                f"### {mode_label}",
                "",
                f"![{workload_title} {mode_label} execution-time CDF]"
                f"(cdf_execution_time_{workload}_{mode}.svg)",
                "",
                f"![{workload_title} {mode_label} P99 peak-RSS bar plot]"
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
            *(
                [
                    "- NVX warm reuse keeps one restored VM and forks each workload "
                    "from the snapshot-initialized CPython parent."
                ]
                if (
                    "nvx" in included_vmms
                    and ("nvx", "warm") in summary_by_key
                )
                else []
            ),
            *snapshot_method_lines,
            *(
                [
                    f"- Snapshot generation records {generation_count_text} sample(s) "
                    "per VMM/workload baseline in this result; `--samples` controls "
                    "restore, runtime-preinitialized resume, and warm reuse."
                ]
                if generation_count_text
                else []
            ),
            *(
                [
                    "- Every snapshot-generation sample writes a dedicated scratch "
                    "snapshot; the harness removes it before starting the timer, validates "
                    "the generated files, removes it after the process, and never mutates "
                    "the reusable restore snapshot."
                ]
                if "snapshot-generation" in mode_order
                else []
            ),
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
        "\nVMM         mode                   n  median-ms   p95-ms  "
        "median-RSS-MiB  p95-RSS-MiB",
        flush=True,
    )
    for summary in summaries:
        elapsed = summary["execution_time_ms"]
        rss = summary["peak_rss_mib"]
        assert isinstance(elapsed, dict) and isinstance(rss, dict)
        print(
            f"{str(summary['vmm']):11s} {str(summary['mode']):22s} "
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
        "--use-docker",
        action="store_true",
        help=(
            "allow Docker-backed builds and use Docker for uncached Hyperlight "
            "workload artifacts instead of the OCI registry API"
        ),
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
    parser.add_argument(
        "--samples",
        type=int,
        default=100,
        help=(
            "samples per restore/runtime-preinitialized/warm target; snapshot "
            f"generation always records {SNAPSHOT_GENERATION_SAMPLES}"
        ),
    )
    parser.add_argument("--seed", type=int, default=0x5EED, help="sample-order seed")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-process timeout")
    parser.add_argument("--cooldown-ms", type=int, default=100)
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument(
        "--workload",
        choices=WORKLOAD_ORDER,
        default="hello",
        help="workload to run (default: hello)",
    )
    parser.add_argument(
        "--vmm",
        action="append",
        choices=VMM_ORDER,
        help="limit to one or more VMM targets supported by the workload; may be repeated",
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
    supported_vmms = set(WORKLOAD_VMMS[args.workload])
    selected = set(args.vmm) if args.vmm else supported_vmms
    unsupported_vmms = selected - supported_vmms
    if unsupported_vmms:
        unsupported = ", ".join(sorted(unsupported_vmms))
        raise ValueError(
            f"workload {args.workload!r} does not support VMM target(s): {unsupported}"
        )
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
        build_projects(selected, use_docker=args.use_docker)
    specs = prepare(
        output_dir,
        selected,
        args.timeout,
        args.workload,
        use_docker=args.use_docker,
    )
    write_metadata(
        output_dir,
        specs,
        args.workload,
        use_docker=args.use_docker,
    )
    if args.prepare_only:
        print(f"Prepared artifacts in {output_dir}")
        return 0

    # Warm reuse runs all iterations in one process per VMM, so pull those
    # specs out of the per-process sampling loop.
    warm_specs = [
        specs.pop(key)
        for key in sorted(
            (key for key in specs if key[1] == "warm"),
            key=lambda key: VMM_ORDER.index(key[0]),
        )
    ]

    run_samples(
        output_dir,
        specs,
        samples=args.samples,
        seed=args.seed,
        timeout=args.timeout,
        cooldown_ms=args.cooldown_ms,
        preflight=not args.no_preflight,
    )

    for warm_spec in warm_specs:
        run_warm_benchmark(
            output_dir,
            warm_spec,
            samples=args.samples,
            timeout=args.timeout,
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
