//! Finite ingestion process: own one durable job, acquire provider data, publish, settle, exit.
mod identity;
mod runtime;

#[tokio::main]
async fn main() {
    let result = async {
        let id = std::env::args().nth(1).ok_or_else(|| {
            lake_contracts::DataError::value("one retained ingestion job ID is required")
        })?;
        runtime::run(&id).await
    }
    .await;
    if let Err(error) = result {
        eprintln!("ingestion worker failed: {error}");
        std::process::exit(1);
    }
}
