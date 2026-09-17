use super::*;

#[test]
fn native_context_exposes_the_duck_typed_surface() {
    Python::attach(|py| {
        let (context, _, _, _) = build_context(
            py,
            vec!["SPY".to_string(), "AAPL".to_string()],
            0,
            "strategy".to_string(),
            PyDecimal::from_i128(1000),
            1729,
        )
        .expect("context");
        let bound = context.bind(py);
        assert_eq!(
            bound
                .getattr("book_id")
                .expect("book_id")
                .extract::<String>()
                .expect("str"),
            "strategy"
        );
        assert!(bound
            .getattr("universe")
            .expect("universe")
            .call_method1("contains", ("SPY",))
            .expect("contains")
            .extract::<bool>()
            .expect("bool"));
        assert!(bound
            .getattr("random")
            .expect("random")
            .hasattr("random")
            .expect("hasattr"));
        assert!(bound
            .getattr("books")
            .expect("books")
            .hasattr("cash")
            .expect("hasattr"));
    });
}

#[test]
fn native_state_store_round_trips_values() {
    Python::attach(|py| {
        let store = Py::new(py, PyStateStore::new()).expect("state");
        let bound = store.bind(py);
        let value = PyList::new(py, [1_i64, 2, 3]).expect("value");
        bound.set_item("entered", &value).expect("set");
        assert!(bound
            .call_method1("__contains__", ("entered",))
            .expect("contains")
            .extract::<bool>()
            .expect("bool"));
        let snapshot = bound.call_method0("snapshot").expect("snapshot");
        assert_eq!(
            snapshot
                .get_item("entered")
                .expect("item")
                .len()
                .expect("len"),
            3
        );
    });
}

#[test]
fn native_context_publishes_only_finite_non_reserved_metrics() {
    Python::attach(|py| {
        let (context, _, _, _) = build_context(
            py,
            vec!["SPY".to_string()],
            0,
            "research".to_string(),
            PyDecimal::from_i128(1000),
            1729,
        )
        .expect("context");
        let bound = context.bind(py);
        bound
            .call_method1("publish_metric", ("rolling_corr_60d_median", 0.73))
            .expect("publish");
        bound
            .call_method1("publish_metric", ("rolling_corr_60d_median", 0.81))
            .expect("replace");

        assert_eq!(
            bound
                .borrow()
                .published_metrics()
                .get("rolling_corr_60d_median"),
            Some(&"0.81".to_string())
        );
        assert!(bound
            .call_method1("publish_metric", ("total_return", 1.0))
            .is_err());
        assert!(bound
            .call_method1("publish_metric", ("not finite", 1.0))
            .is_err());
        assert!(bound
            .call_method1("publish_metric", ("boolean_metric", true))
            .is_err());
        assert!(bound
            .call_method1("publish_metric", ("infinite_metric", f64::INFINITY))
            .is_err());
    });
}
