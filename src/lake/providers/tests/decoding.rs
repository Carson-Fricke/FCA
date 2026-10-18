//! Provider exchanges cover every tick decoder and exact numeric validation.
use axum::{http::Uri, routing::get, Router};
use lake_catalog::{ContractRecord, OptionType};
use lake_contracts::{Date, DateTime, Decimal18};
use lake_providers::{AlpacaClient, ProviderConfig, ProviderError, RequestGovernor};
use std::sync::{Arc, Mutex};

#[tokio::test]
async fn all_tick_families_decode_exactly_and_malformed_pages_fail_at_the_provider_boundary() {
    let response = Arc::new(Mutex::new(String::new()));
    let paths = Arc::new(Mutex::new(Vec::new()));
    let body = response.clone();
    let observed = paths.clone();
    let app = Router::new().fallback(get(move |uri: Uri| {
        let body = body.lock().unwrap().clone();
        observed.lock().unwrap().push(uri.path().to_string());
        async move { body }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let client = AlpacaClient::with_reqwest(
        ProviderConfig::new(base, None, None),
        RequestGovernor::new(None),
    )
    .unwrap();
    let at = DateTime::parse_iso_utc("2025-12-30T14:30:00Z").unwrap();
    let contract = ContractRecord::new(
        "MSFT260116C00200000",
        "MSFT",
        Date::parse("2026-01-16").unwrap(),
        Decimal18::parse("200").unwrap(),
        OptionType::Call,
        100,
        None,
        None,
    )
    .unwrap();
    let price = Decimal18::parse("190.123456789012345678").unwrap();
    let trade = r#"{"t":"2025-12-30T14:30:00Z","i":1,"x":"Q","p":190.123456789012345678,"s":1,"c":["@","I"],"z":"C"}"#;
    let quote = r#"{"t":"2025-12-30T14:30:00Z","bx":"Q","bp":190.123456789012345678,"bs":1,"ax":"P","ap":191,"as":2,"c":["R"],"z":"C"}"#;
    for index in 0..6 {
        let (symbol, kind, row) = match index {
            0 => ("MSFT", "trades", trade.to_string()),
            1 => ("MSFT", "quotes", quote.to_string()),
            2 => (
                "BTC/USD",
                "trades",
                trade.replace("\"s\":1", "\"s\":0.000000000000000001"),
            ),
            3 => ("BTC/USD", "quotes", quote.replace("\"bx\":\"Q\",", "")),
            4 => (
                contract.symbol.as_str(),
                "trades",
                trade.replace("[\"@\",\"I\"]", "\"@\""),
            ),
            _ => (
                contract.symbol.as_str(),
                "quotes",
                quote.replace("[\"R\"]", "\"R\""),
            ),
        };
        *response.lock().unwrap() =
            format!(r#"{{"{kind}":{{"{symbol}":[{row}]}},"next_page_token":null}}"#);
        let actual = match index {
            0 => {
                let rows = client.fetch_trades(&[symbol], at, at).await.unwrap();
                assert_eq!(rows[0].conditions.len(), 2);
                rows[0].price
            }
            1 => client.fetch_quotes(&[symbol], at, at).await.unwrap()[0].bid_price,
            2 => {
                let rows = client.fetch_crypto_trades(&[symbol], at, at).await.unwrap();
                assert_eq!(
                    rows[0].size,
                    Decimal18::parse("0.000000000000000001").unwrap()
                );
                rows[0].price
            }
            3 => {
                let rows = client.fetch_crypto_quotes(&[symbol], at, at).await.unwrap();
                assert_eq!(rows[0].bid_exchange, None);
                rows[0].bid_price
            }
            4 => {
                client
                    .fetch_option_trades(std::slice::from_ref(&contract), at, at)
                    .await
                    .unwrap()[0]
                    .price
            }
            _ => {
                client
                    .fetch_option_quotes(std::slice::from_ref(&contract), at, at)
                    .await
                    .unwrap()[0]
                    .bid_price
            }
        };
        assert_eq!(actual, price);
        assert!(paths.lock().unwrap().last().unwrap().ends_with(kind));
    }
    for row in [
        trade.replace("\"i\":1", "\"i\":1.0"),
        trade.replace("[\"@\",\"I\"]", "null"),
        trade.replace("190.123456789012345678", "\"NaN\""),
    ] {
        *response.lock().unwrap() =
            format!(r#"{{"trades":{{"MSFT":[{row}]}},"next_page_token":null}}"#);
        assert!(matches!(
            client.fetch_trades(&["MSFT"], at, at).await,
            Err(ProviderError::Protocol(_))
        ));
    }
    *response.lock().unwrap() = "invalid JSON".into();
    assert!(matches!(
        client.fetch_trades(&["MSFT"], at, at).await,
        Err(ProviderError::Protocol(_))
    ));
    server.abort();
}
