/// Benchmark runner for hl-uk-mini (Hyperlight + Unikraft).
///
/// Mirrors the hyperlight-runner interface so benchmark.py can drive
/// both VMMs with the same command structure.  Uses hyperlight-unikraft
/// library for sandbox creation, host function registration, and
/// snapshot restore — all benchmark-specific timing stays here.
use std::env;
use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use hyperlight_unikraft::{
    DEFAULT_SCRATCH_MB, SNAPSHOT_TAG,
    create_sandbox, init, restore, run,
    Exec, OciTag, Snapshot,
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

// ── Helpers ─────────────────────────────────────────────────────────────

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1).cloned())
}

// ── Subcommands ─────────────────────────────────────────────────────────

fn capture(initrd: &Path, snapshot_dir: &Path, scratch_mb: usize, warmup: bool) -> Result<()> {
    let (usandbox, _) = create_sandbox(&Some(initrd.to_path_buf()), &None, scratch_mb)?;
    let mut sandbox = init(usandbox)?;
    if warmup {
        run(
            &mut sandbox,
            "import re, xml.etree.ElementTree, zipfile, io, pathlib; pass",
        )?;
    }
    let tag: OciTag = SNAPSHOT_TAG.parse()?;
    let snap = sandbox.snapshot()?;
    snap.save(snapshot_dir, &tag)?;
    println!("SNAPSHOT_OK");
    Ok(())
}

fn snapshot_generation(
    initrd: &Path,
    snapshot_dir: &Path,
    scratch_mb: usize,
    warmup: bool,
) -> Result<()> {
    let t0 = Instant::now();
    let (usandbox, _) = create_sandbox(&Some(initrd.to_path_buf()), &None, scratch_mb)?;
    let mut sandbox = init(usandbox)?;
    if warmup {
        run(
            &mut sandbox,
            "import re, xml.etree.ElementTree, zipfile, io, pathlib; pass",
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

fn cmd_restore(snapshot_dir: &Path, script: &Path) -> Result<()> {
    let tag: OciTag = SNAPSHOT_TAG.parse()?;

    let t0 = Instant::now();
    let snap = Arc::new(Snapshot::load(snapshot_dir, tag)?);
    let mut sandbox = restore(snap)?;
    let load_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let t1 = Instant::now();
    run(&mut sandbox, Exec::File(script.to_path_buf()))?;
    let call_ms = t1.elapsed().as_secs_f64() * 1000.0;

    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6} guest_call_ms={call_ms:.6}");
    println!("BENCHMARK_OK");
    Ok(())
}

fn warm(snapshot_dir: &Path, script: &Path, iterations: usize) -> Result<()> {
    let tag: OciTag = SNAPSHOT_TAG.parse()?;

    // Pre-read script so file I/O doesn't pollute iteration timing.
    let source = fs::read_to_string(script)?;

    let t0 = Instant::now();
    let snap = Arc::new(Snapshot::load(snapshot_dir, tag)?);
    let mut sandbox = restore(snap.clone())?;
    let load_ms = t0.elapsed().as_secs_f64() * 1000.0;
    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6}");

    for i in 0..iterations {
        let t1 = Instant::now();
        run(&mut sandbox, source.as_str())?;
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
fn cold(initrd: &Path, script: &Path, scratch_mb: usize) -> Result<()> {
    let source = fs::read_to_string(script)?;

    let t0 = Instant::now();
    let (usandbox, _) = create_sandbox(&Some(initrd.to_path_buf()), &None, scratch_mb)?;
    let mut sandbox = init(usandbox)?;
    let evolve_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let t1 = Instant::now();
    run(&mut sandbox, source)?;
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
         \n  {program} restore <snapshot-dir> <script>\n\
         \n  {program} warm <snapshot-dir> <script> [--iterations 10]\n\
         \n  {program} cold <initrd> <script> [--scratch-mb 256]"
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

    match args.get(1).map(String::as_str) {
        Some("capture") if args.len() >= 4 => {
            capture(Path::new(&args[2]), Path::new(&args[3]), scratch_mb, warmup)
        }
        Some("snapshot-generation") if args.len() >= 4 => {
            snapshot_generation(Path::new(&args[2]), Path::new(&args[3]), scratch_mb, warmup)
        }
        Some("restore") if args.len() >= 4 => {
            cmd_restore(Path::new(&args[2]), Path::new(&args[3]))
        }
        Some("warm") if args.len() >= 4 => {
            warm(Path::new(&args[2]), Path::new(&args[3]), iterations)
        }
        Some("cold") if args.len() >= 4 => {
            cold(Path::new(&args[2]), Path::new(&args[3]), scratch_mb)
        }
        _ => Err(usage(&args[0]).into()),
    }
}
