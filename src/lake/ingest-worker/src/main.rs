//! Finite ingestion process: own one durable job, acquire provider data, publish, settle, exit.
mod budget;
mod identity;
mod runtime;

#[tokio::main]
async fn main() {
    let result = async {
        let id = std::env::args().nth(1).ok_or_else(|| {
            lake_contracts::DataError::value("one retained ingestion job ID is required")
        })?;
        let partition = std::env::args().nth(2).ok_or_else(|| {
            lake_contracts::DataError::value("an admitted partition key is required")
        })?;
        runtime::run(&id, &partition).await
    }
    .await;
    if let Err(error) = result {
        eprintln!("ingestion worker failed: {error}");
        std::process::exit(1);
    }
}
