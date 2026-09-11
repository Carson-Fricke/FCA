use std::path::PathBuf;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("..")
        .join("..")
}

fn main() {
    let config = repo_root()
        .join("config")
        .join("harness")
        .join("backtest.yaml");
    let out = std::env::temp_dir().join("fca-native-parity");
    let _ = std::fs::remove_dir_all(&out);
    match harness_host::backtest::run_backtest_job(&config, Some(&out)) {
        Ok((outcome, path)) => {
            let actual = std::fs::read(&path).expect("read actual");
            let expected = std::fs::read(
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("..")
                    .join("harness-backtest")
                    .join("tests")
                    .join("golden")
                    .join("backtest_canonical.json"),
            )
            .expect("read golden");
            println!(
                "counts dispatched={} orders={} fills={} risk={}",
                outcome.dispatched_count,
                outcome.order_count,
                outcome.fills.len(),
                outcome.risk_decisions.len()
            );
            println!("hash {}", outcome.dispatch_hash);
            if actual == expected {
                println!("BYTE IDENTICAL");
            } else {
                let mask = |bytes: &[u8]| -> Vec<u8> {
                    let text = String::from_utf8_lossy(bytes).replace("\r\n", "\n");
                    let marker = "\"code_fingerprint\":\"";
                    if let Some(start) = text.find(marker) {
                        let value_start = start + marker.len();
                        let mut out = text.into_bytes();
                        if value_start + 64 <= out.len() {
                            for byte in &mut out[value_start..value_start + 64] {
                                *byte = b'x';
                            }
                        }
                        out
                    } else {
                        text.into_bytes()
                    }
                };
                let masked_expected = mask(&expected);
                let masked_actual = mask(&actual);
                if masked_expected == masked_actual {
                    println!("MASKED BYTE IDENTICAL (only code_fingerprint differs)");
                } else {
                    let limit = masked_actual.len().min(masked_expected.len());
                    let offset = (0..limit)
                        .find(|i| masked_expected[*i] != masked_actual[*i])
                        .unwrap_or(limit);
                    let win = 120;
                    let ge = &masked_expected[offset.min(masked_expected.len())..];
                    let ae = &masked_actual[offset.min(masked_actual.len())..];
                    println!(
                        "MASKED DIFF at {offset} (golden {} actual {})",
                        masked_expected.len(),
                        masked_actual.len()
                    );
                    println!(
                        "golden {:?}",
                        String::from_utf8_lossy(&ge[..ge.len().min(win)])
                    );
                    println!(
                        "actual {:?}",
                        String::from_utf8_lossy(&ae[..ae.len().min(win)])
                    );
                }
            }
        }
        Err(error) => println!("ERROR {error}"),
    }
}
