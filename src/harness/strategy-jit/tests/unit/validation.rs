use super::*;

#[test]
fn validation_cache_keys_full_input_and_identity_and_evicts_at_64() {
    let service = ValidationService::default();
    let model = serde_json::json!({"source": "same", "harnessConfiguration": {"interval": "1d"}});
    let identity = Some("compiler-1".to_string());
    let first = service
        .validate_cached(
            &model,
            identity.clone(),
            || Ok(serde_json::json!({})),
            || identity.clone(),
        )
        .unwrap();
    assert_eq!(first["reused"], false);
    assert_eq!(
        service
            .validate_cached(
                &model,
                identity.clone(),
                || panic!("must skip inspection"),
                || identity.clone()
            )
            .unwrap()["reused"],
        true
    );
    for (input, key) in [
        (
            serde_json::json!({"source": "same", "harnessConfiguration": {"interval": "1min"}}),
            identity.clone(),
        ),
        (model.clone(), Some("compiler-2".into())),
    ] {
        assert_eq!(
            service
                .validate_cached(
                    &input,
                    key.clone(),
                    || Ok(serde_json::json!({})),
                    || key.clone()
                )
                .unwrap()["reused"],
            false
        );
    }
    for index in 0..70 {
        service
            .validate_cached(
                &serde_json::json!({"index": index}),
                identity.clone(),
                || Ok(serde_json::json!({})),
                || identity.clone(),
            )
            .unwrap();
    }
    assert_eq!(service.entries.lock().unwrap().len(), 64);
    assert_eq!(
        service
            .validate_cached(
                &model,
                identity.clone(),
                || Ok(serde_json::json!({})),
                || identity.clone()
            )
            .unwrap()["reused"],
        false
    );
}

#[test]
fn validation_cache_evicts_by_bytes_and_skips_oversized_diagnostics() {
    let service = ValidationService::default();
    let identity = Some("compiler".to_string());
    for index in 0..6 {
        service
            .validate_cached(
                &serde_json::json!({"index": index}),
                identity.clone(),
                || Ok(serde_json::json!({"detail": "x".repeat(1024 * 1024)})),
                || identity.clone(),
            )
            .unwrap();
    }
    let entries = service.entries.lock().unwrap();
    assert!(entries.len() < 6);
    assert!(
        entries
            .iter()
            .map(|(key, result)| validation_result_bytes(key, result))
            .sum::<usize>()
            <= VALIDATION_CACHE_BYTES
    );
    assert!(!entries.iter().any(|(key, _)| key.ends_with(&digest_hex(
        serde_json::json!({"index": 0}).to_string().as_bytes()
    ))));
    drop(entries);
    for _ in 0..2 {
        let error = service
            .validate_cached(
                &serde_json::json!({"oversized": true}),
                identity.clone(),
                || {
                    Err(ValidationFailure::from(JitError::Compile {
                        digest: "digest".into(),
                        detail: "authored error".into(),
                        diagnostics: vec![
                            serde_json::json!({"rendered": "x".repeat(VALIDATION_CACHE_BYTES)}),
                        ],
                    }))
                },
                || identity.clone(),
            )
            .unwrap_err();
        assert!(!error.reused);
    }
    assert!(
        service
            .entries
            .lock()
            .unwrap()
            .iter()
            .map(|(key, result)| validation_result_bytes(key, result))
            .sum::<usize>()
            <= VALIDATION_CACHE_BYTES
    );
}

#[test]
fn deterministic_errors_reuse_but_infrastructure_and_unknown_identity_never_do() {
    let service = ValidationService::default();
    let model = serde_json::json!({"source": "broken"});
    let identity = Some("compiler".to_string());
    let authored = || {
        Err(ValidationFailure::from(JitError::Compile {
            digest: "digest".into(),
            detail: "rendered error".into(),
            diagnostics: vec![serde_json::json!({"level": "error", "code": {"code": "E0308"}})],
        }))
    };
    let first = service
        .validate_cached(&model, identity.clone(), authored, || identity.clone())
        .unwrap_err();
    assert!(first.deterministic_compiler_diagnostic);
    assert!(!first.reused);
    let second = service
        .validate_cached(
            &model,
            identity.clone(),
            || panic!("must reuse compile error"),
            || identity.clone(),
        )
        .unwrap_err();
    assert!(second.reused);
    assert_eq!(second.diagnostics, first.diagnostics);
    for index in 0..2 {
        let infra = service
            .validate_cached(
                &serde_json::json!({"infra": true}),
                identity.clone(),
                || Err(ValidationFailure::new(format!("io-{index}"))),
                || identity.clone(),
            )
            .unwrap_err();
        assert_eq!(infra.message, format!("io-{index}"));
        assert!(!infra.cacheable);
        assert!(!infra.deterministic_compiler_diagnostic);
    }
    let config_model = serde_json::json!({"config": true});
    service
        .validate_cached(
            &config_model,
            identity.clone(),
            || Err(ValidationFailure::configuration("invalid config")),
            || identity.clone(),
        )
        .unwrap_err();
    let config = service
        .validate_cached(
            &config_model,
            identity.clone(),
            || panic!("reuse configuration rejection"),
            || identity.clone(),
        )
        .unwrap_err();
    assert!(config.reused);
    assert!(!config.deterministic_compiler_diagnostic);
    for _ in 0..2 {
        let uncached = service
            .validate_cached(&model, None, authored, || None)
            .unwrap_err();
        assert!(!uncached.reused);
        assert!(!uncached.cacheable);
    }
    let changing = serde_json::json!({"changed-during-inspect": true});
    let unstable = service
        .validate_cached(
            &changing,
            identity.clone(),
            || Ok(serde_json::json!({})),
            || Some("new".into()),
        )
        .unwrap();
    assert_eq!(unstable["cacheable"], false);
    assert_eq!(
        service
            .validate_cached(
                &changing,
                identity.clone(),
                || Ok(serde_json::json!({})),
                || identity.clone()
            )
            .unwrap()["reused"],
        false
    );
}
