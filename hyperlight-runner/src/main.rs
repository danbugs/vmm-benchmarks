use hyperlight_unikraft::pyhl::configure_surrogates;
use hyperlight_unikraft::Sandbox;
use std::env;
use std::error::Error;
use std::fs;
use std::path::Path;
use std::time::Instant;

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const PYTHON_HEAP_SIZE: u64 = 2560 * 1024 * 1024;
const GENERIC_WARMUP: &str = "pass";

fn cold(kernel: &Path, initrd: &Path, script: &Path) -> Result<()> {
    let source = fs::read_to_string(script)?;
    let build_started = Instant::now();
    let mut sandbox = Sandbox::builder(kernel)
        .initrd_file(initrd)
        .heap_size(PYTHON_HEAP_SIZE)
        .build()?;
    let build_ms = build_started.elapsed().as_secs_f64() * 1000.0;
    let rewind_started = Instant::now();
    sandbox.restore()?;
    let rewind_ms = rewind_started.elapsed().as_secs_f64() * 1000.0;
    let call_started = Instant::now();
    let _: () = sandbox.call_named("run", source)?;
    let call_ms = call_started.elapsed().as_secs_f64() * 1000.0;
    println!(
        "BENCHMARK_PHASE sandbox_build_ms={build_ms:.6} \
         initial_rewind_ms={rewind_ms:.6} guest_call_ms={call_ms:.6}"
    );
    println!("BENCHMARK_OK");
    Ok(())
}

fn capture(kernel: &Path, initrd: &Path, snapshot: &Path) -> Result<()> {
    let mut sandbox = Sandbox::builder(kernel)
        .initrd_file(initrd)
        .heap_size(PYTHON_HEAP_SIZE)
        .build()?;
    sandbox.restore()?;
    let _: () = sandbox.call_named("run", GENERIC_WARMUP.to_string())?;
    sandbox.snapshot_now()?;
    sandbox.save_snapshot(snapshot)?;
    println!("SNAPSHOT_OK");
    Ok(())
}

fn restore(initrd: &Path, snapshot: &Path, script: &Path) -> Result<()> {
    let source = fs::read_to_string(script)?;
    let load_started = Instant::now();
    let mut sandbox =
        Sandbox::from_snapshot_file_configured(snapshot, &[], Some(initrd), None, None)?;
    let load_ms = load_started.elapsed().as_secs_f64() * 1000.0;
    let call_started = Instant::now();
    let _: () = sandbox.call_named("run", source)?;
    let call_ms = call_started.elapsed().as_secs_f64() * 1000.0;
    println!("BENCHMARK_PHASE snapshot_load_ms={load_ms:.6} guest_call_ms={call_ms:.6}");
    println!("BENCHMARK_OK");
    Ok(())
}

fn usage(program: &str) -> String {
    format!(
        "usage:\n  {program} cold <kernel> <initrd> <script>\n  \
         {program} capture <kernel> <initrd> <snapshot-dir>\n  \
         {program} restore <initrd> <snapshot-dir> <script>"
    )
}

fn main() -> Result<()> {
    configure_surrogates(Some(0));

    let args: Vec<String> = env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("cold") if args.len() == 5 => cold(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
        ),
        Some("capture") if args.len() == 5 => capture(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
        ),
        Some("restore") if args.len() == 5 => restore(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
        ),
        _ => Err(usage(&args[0]).into()),
    }
}
