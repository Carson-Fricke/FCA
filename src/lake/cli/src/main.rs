//! The lake development-data cleanup entrypoint.

fn main() {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    std::process::exit(lake_cli::run(arguments));
}
