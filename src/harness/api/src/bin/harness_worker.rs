//! Finite execution, scheduled recovery, isolated validation or durable smoke.
fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let result = match args.as_slice() {
        [] => api::execution::worker::run().map_err(|e| e.to_string()),
        [mode] if mode == "--dispatch" => {
            api::execution::worker::dispatch().map_err(|e| e.to_string())
        }
        [mode] if mode == "--validate" => api::execution::validation::serve(),
        [mode] if mode == "--smoke" => api::execution::worker::smoke().map_err(|e| e.to_string()),
        _ => Err("expected no arguments, --dispatch, --validate or --smoke".into()),
    };
    match result {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("harness worker: {error}");
            std::process::ExitCode::from(2)
        }
    }
}
