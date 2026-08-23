/// Benchmark runner for hl-uk-mini (Hyperlight + Unikraft).
///
/// Mirrors the hyperlight-runner interface so benchmark.py can drive
/// both VMMs with the same command structure.  Uses the lower-level
/// hyperlight-host API (GuestBinary::Buffer, Snapshot, map_file_cow)
/// instead of the hyperlight-unikraft Sandbox builder.
use std::env;
use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use hyperlight_host::{
    GuestBinary, HostFunctions, MultiUseSandbox, UninitializedSandbox,
    func::Registerable,
    sandbox::{SandboxConfiguration, snapshot::{OciTag, Snapshot}},
    sandbox::uninitialized::GuestEnvironment,
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

// ── Embedded kernel ─────────────────────────────────────────────────────

static KERNEL: &[u8] = include_bytes!("../kernel/elfloader_hyperlight-x86_64");

// ── Constants ───────────────────────────────────────────────────────────

const INITRD_MAP_BASE: u64 = 0xFEF0_0000;
const DEFAULT_SCRATCH_MB: usize = 256;
const HEAP_SIZE: u64 = 0x10_0000; // 1 MiB
const SNAPSHOT_TAG: &str = "latest";

// TLV magic headers for init_data — must match the Unikraft platform code.
const CMDLINE_MAGIC: &[u8; 8] = b"HLCMDLN\0";
const WALLTIME_MAGIC: &[u8; 8] = b"HLWALL0\0";

/// Build an init_data blob containing the cmdline TLV and wall-clock TLV.
/// The Unikraft guest reads these at boot to set the command line and
/// initialize the wall clock (so `time.localtime()` returns the right date).
fn build_init_data(cmdline: &str) -> Vec<u8> {
    let mut buf = Vec::new();

    // HLCMDLN TLV
    let cmdline_bytes = cmdline.as_bytes();
    buf.extend_from_slice(CMDLINE_MAGIC);
    buf.extend_from_slice(&(cmdline_bytes.len() as u32).to_le_bytes());
    buf.extend_from_slice(cmdline_bytes);
    buf.push(0);

    // HLWALL0 TLV — host wall time so the guest has a sensible epoch
    let wall_ns = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    buf.extend_from_slice(WALLTIME_MAGIC);
    buf.extend_from_slice(&8u32.to_le_bytes());
    buf.extend_from_slice(&wall_ns.to_le_bytes());

    buf
}

// ── Host functions ──────────────────────────────────────────────────────

fn paging_budget(scratch_size: usize) -> u64 {
    (scratch_size as u64) * 3 / 4
}

fn exn_stack_top() -> u64 {
    hyperlight_common::layout::SCRATCH_TOP_GVA as u64
        - hyperlight_common::layout::SCRATCH_TOP_EXN_STACK_OFFSET
        + 1
}

/// Scan a newc-format CPIO for `usr/local/bin/hl_*` or `usr/bin/hl_*`.
fn find_cpio_entry(path: &Path) -> Option<String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = std::fs::File::open(path).ok()?;
    let mut header = [0u8; 110];
    loop {
        if file.read_exact(&mut header).is_err() { break; }
        let magic = std::str::from_utf8(&header[0..6]).ok()?;
        if magic != "070701" && magic != "070702" { break; }
        let namesize = u32::from_str_radix(std::str::from_utf8(&header[94..102]).ok()?, 16).ok()?;
        let filesize = u64::from_str_radix(std::str::from_utf8(&header[54..62]).ok()?, 16).ok()?;
        let mut name_buf = vec![0u8; namesize as usize];
        file.read_exact(&mut name_buf).ok()?;
        let name = std::str::from_utf8(&name_buf).ok()?.trim_end_matches('\0');
        if name == "TRAILER!!!" { break; }
        let name_padding = (4 - ((110 + namesize) % 4)) % 4;
        file.seek(SeekFrom::Current(name_padding as i64)).ok()?;
        if name.starts_with("usr/local/bin/hl_") || name.starts_with("usr/bin/hl_") {
            return Some(format!("/{name}"));
        }
        let data_padding = (4 - (filesize % 4)) % 4;
        file.seek(SeekFrom::Current((filesize + data_padding) as i64)).ok()?;
    }
    None
}

fn register_host_functions(
    sandbox: &mut UninitializedSandbox,
    cmdline: &str,
    scratch_size: usize,
    initrd_base: u64,
    initrd_size: u64,
) -> Result<()> {
    let cmdline = cmdline.to_string();
    sandbox.register("GetCmdLine", move || -> hyperlight_host::Result<String> {
        Ok(cmdline.clone())
    })?;
    let budget = paging_budget(scratch_size);
    sandbox.register("GetPagingBudget", move || -> hyperlight_host::Result<u64> { Ok(budget) })?;
    sandbox.register("GetInitrdBase", move || -> hyperlight_host::Result<u64> { Ok(initrd_base) })?;
    sandbox.register("GetInitrdSize", move || -> hyperlight_host::Result<u64> { Ok(initrd_size) })?;
    let est = exn_stack_top();
    sandbox.register("GetExnStackTop", move || -> hyperlight_host::Result<u64> { Ok(est) })?;
    Ok(())
}

fn build_host_functions() -> Result<HostFunctions> {
    let mut hf = HostFunctions::default();
    hf.register_host_function("GetCmdLine", || -> hyperlight_host::Result<String> { Ok(String::new()) })?;
    hf.register_host_function("GetPagingBudget", || -> hyperlight_host::Result<u64> { Ok(0) })?;
    hf.register_host_function("GetInitrdBase", || -> hyperlight_host::Result<u64> { Ok(0) })?;
    hf.register_host_function("GetInitrdSize", || -> hyperlight_host::Result<u64> { Ok(0) })?;
    hf.register_host_function("GetExnStackTop", || -> hyperlight_host::Result<u64> { Ok(exn_stack_top()) })?;
    Ok(hf)
}

// ── Helpers ─────────────────────────────────────────────────────────────

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1).cloned())
}

fn evolve_sandbox(initrd: &Path, scratch_mb: usize) -> Result<MultiUseSandbox> {
    let scratch_size = scratch_mb * 1024 * 1024;
    let mut cfg = SandboxConfiguration::default();
    cfg.set_scratch_size(scratch_size);
    cfg.set_heap_size(HEAP_SIZE);

    let entry = find_cpio_entry(initrd).unwrap_or_default();
    let cmdline = if entry.is_empty() {
        "unikraft-hyperlight".to_string()
    } else {
        format!("unikraft-hyperlight {entry}")
    };

    let init_data = build_init_data(&cmdline);
    let env = GuestEnvironment::new(GuestBinary::Buffer(KERNEL), Some(&init_data));
    let mut usandbox = UninitializedSandbox::new(env, Some(cfg))?;

    let initrd_size = usandbox.map_file_cow(initrd, INITRD_MAP_BASE)?;

    register_host_functions(&mut usandbox, &cmdline, scratch_size, INITRD_MAP_BASE, initrd_size)?;
    let sandbox = usandbox.evolve()?;
    Ok(sandbox)
}

// ── Subcommands ─────────────────────────────────────────────────────────

fn capture(initrd: &Path, snapshot_dir: &Path, scratch_mb: usize, warmup: bool) -> Result<()> {
    let mut sandbox = evolve_sandbox(initrd, scratch_mb)?;
    if warmup {
        // Warmup: import heavy modules so that demand-paged memory is
        // faulted in before snapshotting.  Without this, snapshot
        // restore + heavy scripts crash because the demand pager can't
        // map new pages after restore.
        sandbox.call::<()>(
            "Exec",
            "import re, xml.etree.ElementTree, zipfile, io, pathlib; pass".to_string(),
        )?;
    }
    let tag: OciTag = SNAPSHOT_TAG.parse()?;
    let snap = sandbox.snapshot()?;
    snap.save(snapshot_dir, &tag)?;
    println!("SNAPSHOT_OK");
    Ok(())
}

fn snapshot_generation(initrd: &Path, snapshot_dir: &Path, scratch_mb: usize, warmup: bool) -> Result<()> {
    let scratch_size = scratch_mb * 1024 * 1024;
    let mut cfg = SandboxConfiguration::default();
    cfg.set_scratch_size(scratch_size);
    cfg.set_heap_size(HEAP_SIZE);

    let entry = find_cpio_entry(initrd).unwrap_or_default();
    let cmdline = if entry.is_empty() {
        "unikraft-hyperlight".to_string()
    } else {
        format!("unikraft-hyperlight {entry}")
    };

    let t0 = Instant::now();
    let init_data = build_init_data(&cmdline);
    let env = GuestEnvironment::new(GuestBinary::Buffer(KERNEL), Some(&init_data));
    let mut usandbox = UninitializedSandbox::new(env, Some(cfg))?;

    let initrd_size = usandbox.map_file_cow(initrd, INITRD_MAP_BASE)?;
    register_host_functions(&mut usandbox, &cmdline, scratch_size, INITRD_MAP_BASE, initrd_size)?;
    let mut sandbox = usandbox.evolve()?;
    if warmup {
        sandbox.call::<()>(
            "Exec",
            "import re, xml.etree.ElementTree, zipfile, io, pathlib; pass".to_string(),
        )?;
    }
    let sandbox_build_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let t1 = Instant::now();
    let snap = sandbox.snapshot()?;
    let snapshot_capture_ms = t1.elapsed().as_secs_f64() * 1000.0;

    let tag: OciTag = SNAPSHOT_TAG.parse()?;
    let t2 = Instant::now();
    snap.save(snapshot_dir, &tag)?;
    let snapshot_persist_ms = t2.elapsed().as_secs_f64() * 1000.0;

    println!(
        "BENCHMARK_PHASE sandbox_build_ms={sandbox_build_ms:.6} \
         snapshot_capture_ms={snapshot_capture_ms:.6} \
         snapshot_persist_ms={snapshot_persist_ms:.6}"
    );
    println!("BENCHMARK_OK");
    Ok(())
}

fn restore(snapshot_dir: &Path, initrd: Option<&Path>, script: Option<&Path>) -> Result<()> {
    let tag: OciTag = SNAPSHOT_TAG.parse()?;

    let t0 = Instant::now();
    let snap: Arc<Snapshot> = Arc::new(Snapshot::load(snapshot_dir, tag)?);
    let hf = build_host_functions()?;
    let mut sandbox = MultiUseSandbox::from_snapshot(snap, hf, None)?;
    // Re-map initrd so demand-paged pages not faulted during capture
    // can still be resolved after restore.
    if let Some(initrd_path) = initrd {
        sandbox.map_file_cow(initrd_path, INITRD_MAP_BASE)?;
    }
    let load_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let t1 = Instant::now();
    match script {
        Some(path) => {
            let source = fs::read_to_string(path)?;
            sandbox.call::<()>("Exec", source)?;
        }
        None => {
            // Non-script workloads (e.g. Node.js): the app runs to
            // completion when we poke the guest dispatch.
            sandbox.call::<()>("run", ())?;
        }
    }
    let call_ms = t1.elapsed().as_secs_f64() * 1000.0;

    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6} guest_call_ms={call_ms:.6}");
    println!("BENCHMARK_OK");
    Ok(())
}

fn warm(snapshot_dir: &Path, initrd: Option<&Path>, script: Option<&Path>, iterations: usize) -> Result<()> {
    let tag: OciTag = SNAPSHOT_TAG.parse()?;

    let source = script.map(fs::read_to_string).transpose()?;

    let t0 = Instant::now();
    let snap: Arc<Snapshot> = Arc::new(Snapshot::load(snapshot_dir, tag)?);
    let hf = build_host_functions()?;
    let mut sandbox = MultiUseSandbox::from_snapshot(snap.clone(), hf, None)?;
    if let Some(initrd_path) = initrd {
        sandbox.map_file_cow(initrd_path, INITRD_MAP_BASE)?;
    }
    let load_ms = t0.elapsed().as_secs_f64() * 1000.0;
    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6}");

    for i in 0..iterations {
        let t1 = Instant::now();
        match &source {
            Some(src) => sandbox.call::<()>("Exec", src.clone())?,
            None => sandbox.call::<()>("run", ())?,
        }
        let call_ms = t1.elapsed().as_secs_f64() * 1000.0;

        let t2 = Instant::now();
        sandbox.restore(snap.clone())?;
        let rewind_ms = t2.elapsed().as_secs_f64() * 1000.0;

        println!(
            "BENCHMARK_PHASE warm_iteration={i} guest_call_ms={call_ms:.6} \
             rewind_ms={rewind_ms:.6}"
        );
    }
    println!("BENCHMARK_OK");
    Ok(())
}

/// Cold start: fresh evolve + dispatch (no snapshot).
/// Used for workloads where snapshot/restore doesn't work (e.g. Node.js).
fn cold(initrd: &Path, script: Option<&Path>, scratch_mb: usize) -> Result<()> {
    let source = match script {
        Some(path) => fs::read_to_string(path)?,
        None => return Err("cold requires a script argument".into()),
    };

    let t0 = Instant::now();
    let mut sandbox = evolve_sandbox(initrd, scratch_mb)?;
    let evolve_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let t1 = Instant::now();
    sandbox.call::<()>("Exec", source)?;
    let call_ms = t1.elapsed().as_secs_f64() * 1000.0;

    println!("BENCHMARK_PHASE sandbox_build_ms={evolve_ms:.6} guest_call_ms={call_ms:.6}");
    println!("BENCHMARK_OK");
    Ok(())
}

// ── CLI ─────────────────────────────────────────────────────────────────

fn usage(program: &str) -> String {
    format!(
        "hl-uk-mini benchmark runner\n\n\
         usage:\n\
         \n  {program} capture <initrd> <snapshot-dir> [--scratch-mb 256] [--warmup]\n\
         \n  {program} snapshot-generation <initrd> <snapshot-dir> [--scratch-mb 256] [--warmup]\n\
         \n  {program} restore <snapshot-dir> [<script>] [--initrd <path>]\n\
         \n  {program} warm <snapshot-dir> [<script>] [--iterations 10] [--initrd <path>]\n\
         \n  {program} cold <initrd> [<script>] [--scratch-mb 256]"
    )
}

fn main() -> Result<()> {
    // Disable surrogate processes — they add seconds to snapshot load
    // by mapping all sandbox memory into a separate process.
    #[cfg(target_os = "windows")]
    unsafe {
        env::set_var("HYPERLIGHT_MAX_SURROGATES", "0");
        env::set_var("HYPERLIGHT_INITIAL_SURROGATES", "0");
    }

    let args: Vec<String> = env::args().collect();
    let scratch_mb: usize = flag_value(&args, "--scratch-mb")
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_SCRATCH_MB);
    let iterations: usize = flag_value(&args, "--iterations")
        .and_then(|v| v.parse().ok())
        .unwrap_or(1);
    let warmup = args.iter().any(|a| a == "--warmup");
    let initrd = flag_value(&args, "--initrd").map(|v| std::path::PathBuf::from(v));

    match args.get(1).map(String::as_str) {
        Some("capture") if args.len() >= 4 => {
            capture(Path::new(&args[2]), Path::new(&args[3]), scratch_mb, warmup)
        }
        Some("snapshot-generation") if args.len() >= 4 => {
            snapshot_generation(Path::new(&args[2]), Path::new(&args[3]), scratch_mb, warmup)
        }
        Some("restore") if args.len() >= 3 => {
            let script = args.get(3)
                .filter(|s| !s.starts_with("--"))
                .map(|s| Path::new(s.as_str()));
            restore(Path::new(&args[2]), initrd.as_deref(), script)
        }
        Some("warm") if args.len() >= 3 => {
            let script = args.get(3)
                .filter(|s| !s.starts_with("--"))
                .map(|s| Path::new(s.as_str()));
            warm(Path::new(&args[2]), initrd.as_deref(), script, iterations)
        }
        Some("cold") if args.len() >= 3 => {
            let script = args.get(3)
                .filter(|s| !s.starts_with("--"))
                .map(|s| Path::new(s.as_str()));
            cold(Path::new(&args[2]), script, scratch_mb)
        }
        _ => Err(usage(&args[0]).into()),
    }
}
