use hyperlight_unikraft::pyhl::configure_surrogates;
use hyperlight_unikraft::Sandbox;
use std::env;
use std::error::Error;
use std::fs;
use std::path::Path;
use std::time::Instant;

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const DEFAULT_HEAP_SIZE: u64 = 2560 * 1024 * 1024;

struct SnapshotTimings {
    sandbox_build_ms: f64,
    initial_rewind_ms: f64,
    warmup_ms: Option<f64>,
    snapshot_capture_ms: f64,
    snapshot_persist_ms: f64,
}

fn has_flag(args: &[String], flag: &str) -> bool {
    args.iter().any(|a| a == flag)
}

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1).cloned())
}

fn parse_memory(s: &str) -> Result<u64> {
    let s = s.trim();
    if let Some(n) = s.strip_suffix("Mi") {
        Ok(n.parse::<u64>()? * 1024 * 1024)
    } else if let Some(n) = s.strip_suffix("Gi") {
        Ok(n.parse::<u64>()? * 1024 * 1024 * 1024)
    } else {
        Ok(s.parse::<u64>()?)
    }
}

fn create_snapshot(
    kernel: &Path,
    initrd: &Path,
    snapshot: &Path,
    heap_size: u64,
    warmup: bool,
    app_args: Vec<String>,
) -> Result<SnapshotTimings> {
    let build_started = Instant::now();
    let mut sandbox = Sandbox::builder(kernel)
        .initrd_file(initrd)
        .heap_size(heap_size)
        .args(app_args)
        .build()?;
    let sandbox_build_ms = build_started.elapsed().as_secs_f64() * 1000.0;

    let rewind_started = Instant::now();
    sandbox.restore()?;
    let initial_rewind_ms = rewind_started.elapsed().as_secs_f64() * 1000.0;

    let warmup_ms = if warmup {
        let warmup_started = Instant::now();
        let _: () = sandbox.call_named("run", "pass".to_string())?;
        Some(warmup_started.elapsed().as_secs_f64() * 1000.0)
    } else {
        None
    };

    let capture_started = Instant::now();
    sandbox.snapshot_now()?;
    let snapshot_capture_ms = capture_started.elapsed().as_secs_f64() * 1000.0;

    let persist_started = Instant::now();
    sandbox.save_snapshot(snapshot)?;
    let snapshot_persist_ms = persist_started.elapsed().as_secs_f64() * 1000.0;

    Ok(SnapshotTimings {
        sandbox_build_ms,
        initial_rewind_ms,
        warmup_ms,
        snapshot_capture_ms,
        snapshot_persist_ms,
    })
}

// ── Prepare (untimed snapshot creation) ─────────────────────────────────

fn capture(
    kernel: &Path,
    initrd: &Path,
    snapshot: &Path,
    heap_size: u64,
    warmup: bool,
    app_args: Vec<String>,
) -> Result<()> {
    create_snapshot(kernel, initrd, snapshot, heap_size, warmup, app_args)?;
    println!("SNAPSHOT_OK");
    Ok(())
}

// ── Measured snapshot generation ────────────────────────────────────────

fn snapshot_generation(
    kernel: &Path,
    initrd: &Path,
    snapshot: &Path,
    heap_size: u64,
    warmup: bool,
    app_args: Vec<String>,
) -> Result<()> {
    let timings = create_snapshot(kernel, initrd, snapshot, heap_size, warmup, app_args)?;
    let warmup_phase = timings
        .warmup_ms
        .map(|ms| format!(" guest_call_ms={ms:.6}"))
        .unwrap_or_default();
    println!(
        "BENCHMARK_PHASE sandbox_build_ms={:.6} initial_rewind_ms={:.6}{} \
         snapshot_capture_ms={:.6} snapshot_persist_ms={:.6}",
        timings.sandbox_build_ms,
        timings.initial_rewind_ms,
        warmup_phase,
        timings.snapshot_capture_ms,
        timings.snapshot_persist_ms,
    );
    println!("BENCHMARK_OK");
    Ok(())
}

// ── Persisted snapshot resume ───────────────────────────────────────────

fn restore(initrd: &Path, snapshot: &Path, script: Option<&Path>) -> Result<()> {
    let load_started = Instant::now();
    let mut sandbox =
        Sandbox::from_snapshot_file_configured(snapshot, &[], Some(initrd), None, None)?;
    let load_ms = load_started.elapsed().as_secs_f64() * 1000.0;

    let call_started = Instant::now();
    match script {
        Some(path) => {
            let source = fs::read_to_string(path)?;
            let _: () = sandbox.call_named("run", source)?;
        }
        None => sandbox.call_run()?,
    }
    let call_ms = call_started.elapsed().as_secs_f64() * 1000.0;

    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6} guest_call_ms={call_ms:.6}");
    println!("BENCHMARK_OK");
    Ok(())
}

// ── Warm reuse ──────────────────────────────────────────────────────────

fn warm(initrd: &Path, snapshot: &Path, script: &Path, iterations: usize) -> Result<()> {
    let source = fs::read_to_string(script)?;
    let load_started = Instant::now();
    let mut sandbox =
        Sandbox::from_snapshot_file_configured(snapshot, &[], Some(initrd), None, None)?;
    let load_ms = load_started.elapsed().as_secs_f64() * 1000.0;
    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6}");

    for i in 0..iterations {
        let call_started = Instant::now();
        let _: () = sandbox.call_named("run", source.clone())?;
        let call_ms = call_started.elapsed().as_secs_f64() * 1000.0;
        let rewind_started = Instant::now();
        sandbox.restore()?;
        let rewind_ms = rewind_started.elapsed().as_secs_f64() * 1000.0;
        println!(
            "BENCHMARK_PHASE warm_iteration={i} guest_call_ms={call_ms:.6} \
             rewind_ms={rewind_ms:.6}"
        );
    }
    println!("BENCHMARK_OK");
    Ok(())
}

fn usage(program: &str) -> String {
    format!(
        "usage:\n\
         \n  Prepare (untimed snapshot creation):\n  \
         {program} capture <kernel> <initrd> <snapshot-dir> [--warmup] [--heap-size 2560Mi] [-- app-args]\n\
         \n  Snapshot generation (create and persist a measured scratch snapshot):\n  \
         {program} snapshot-generation <kernel> <initrd> <snapshot-dir> [--warmup] [--heap-size 2560Mi] [-- app-args]\n\
         \n  Persisted snapshot resume (load snapshot + execute):\n  \
         {program} restore <initrd> <snapshot-dir> [<script>]\n\
         \n  Warm reuse (reuse partition, re-call without reload):\n  \
         {program} warm <initrd> <snapshot-dir> <script> [--iterations 10]"
    )
}

fn main() -> Result<()> {
    configure_surrogates(Some(0));

    let args: Vec<String> = env::args().collect();
    let heap_size = flag_value(&args, "--heap-size")
        .map(|v| parse_memory(&v))
        .transpose()?
        .unwrap_or(DEFAULT_HEAP_SIZE);
    let iterations: usize = flag_value(&args, "--iterations")
        .and_then(|v| v.parse().ok())
        .unwrap_or(1);
    let warmup = has_flag(&args, "--warmup");

    let dashdash = args.iter().position(|a| a == "--");
    let app_args: Vec<String> = match dashdash {
        Some(pos) => args[pos + 1..].to_vec(),
        None => Vec::new(),
    };

    match args.get(1).map(String::as_str) {
        Some("capture") if args.len() >= 5 => capture(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
            heap_size,
            warmup,
            app_args,
        ),
        Some("snapshot-generation") if args.len() >= 5 => snapshot_generation(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
            heap_size,
            warmup,
            app_args,
        ),
        Some("restore") if args.len() >= 4 => {
            let script = args.get(4).map(|s| Path::new(s.as_str()));
            restore(Path::new(&args[2]), Path::new(&args[3]), script)
        }
        Some("warm") if args.len() >= 5 => warm(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
            iterations,
        ),
        _ => Err(usage(&args[0]).into()),
    }
}
