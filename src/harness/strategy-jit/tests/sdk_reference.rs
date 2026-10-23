use harness_strategy_jit::validation_service;
use serde_json::json;
use serde_json::Map;
use std::collections::BTreeMap;

#[test]
fn sdk_identity_matches_workspace_programs_relative_source_hash_contract() {
    let sdk = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../strategy-sdk");
    let mut sources = BTreeMap::new();
    for (name, root) in [
        ("strategy-sdk", sdk.clone()),
        ("contracts", sdk.join("../contracts")),
        ("num", sdk.join("../num")),
        ("lake-contracts", sdk.join("../../lake/contracts")),
    ] {
        sources.insert(
            format!("{name}/Cargo.toml"),
            harness_contracts::sha256::digest_hex(
                std::fs::read_to_string(root.join("Cargo.toml"))
                    .unwrap()
                    .replace("\r\n", "\n")
                    .as_bytes(),
            ),
        );
        let mut directories = vec![root.join("src")];
        while let Some(directory) = directories.pop() {
            for entry in std::fs::read_dir(directory).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    directories.push(path);
                    continue;
                }
                if path.is_file() && path.extension().is_some_and(|extension| extension == "rs") {
                    sources.insert(
                        format!(
                            "{name}/{}",
                            path.strip_prefix(&root)
                                .unwrap()
                                .to_str()
                                .unwrap()
                                .replace('\\', "/")
                        ),
                        harness_contracts::sha256::digest_hex(
                            std::fs::read_to_string(&path)
                                .unwrap()
                                .replace("\r\n", "\n")
                                .as_bytes(),
                        ),
                    );
                }
            }
        }
    }
    let expected = harness_contracts::sha256::digest_hex(&serde_json::to_vec(&sources).unwrap());
    let result = validation_service().sdk_reference(&Map::new()).unwrap();
    assert_eq!(result["sdkIdentity"], expected);
    assert_eq!(result["sourceHashes"], json!(sources));
    assert_eq!(result["sdkVersion"], harness_strategy_sdk::SDK_VERSION);
    assert_eq!(
        result["sdkAbiVersion"],
        harness_strategy_sdk::SDK_ABI_VERSION
    );
    assert_eq!(
        result["validationIdentity"],
        validation_service().validation_identity().unwrap()
    );
    let args = json!({"topic": "consumer", "start_line": 1, "end_line": 3, "max_chars": 8});
    let reference = validation_service()
        .sdk_reference(args.as_object().unwrap())
        .unwrap();
    assert_eq!(reference["sdkIdentity"], result["sdkIdentity"]);
    assert_eq!(reference["content"].as_str().unwrap().chars().count(), 8);
    assert_eq!(reference["truncated"], true);
    let decimal = validation_service()
        .sdk_reference(
            json!({"topic": "decimal", "start_line": 1, "end_line": 40})
                .as_object()
                .unwrap(),
        )
        .unwrap();
    assert_eq!(decimal["sdkIdentity"], result["sdkIdentity"]);
    assert_eq!(decimal["topic"], "decimal");
    assert!(decimal["content"].as_str().unwrap().contains("to_f64"));
    for topic in [
        "research_views",
        "research_publishing",
        "research_recipes",
        "wavelet",
    ] {
        let reference = validation_service()
            .sdk_reference(
                json!({"topic":topic,"start_line":1,"end_line":3})
                    .as_object()
                    .unwrap(),
            )
            .unwrap();
        assert_eq!(reference["sdkIdentity"], result["sdkIdentity"]);
        assert!(!reference["content"].as_str().unwrap().is_empty());
    }
    for invalid in [
        json!({"topic": "../../secret"}),
        json!({"topic": "consumer", "max_chars": 16001}),
        json!({"topic": "consumer", "start_line": 0}),
    ] {
        assert!(validation_service()
            .sdk_reference(invalid.as_object().unwrap())
            .is_err());
    }
}
