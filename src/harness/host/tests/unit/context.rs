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
