use super::{Fill, VirtualBook};
use crate::decimal::{from_int, mul_div, ONE};
use crate::market::Side;

fn book_fill<'a>(
    fill_id: &'a str,
    side: Side,
    quantity: i128,
    price: i128,
    fee: i128,
    symbol: &'a str,
) -> Fill<'a> {
    Fill {
        fill_id,
        order_id: "order-x",
        symbol,
        side,
        quantity,
        price,
        fee,
    }
}

#[test]
fn average_cost_partial_close_reversal_and_fees_are_exact() {
    let mut book = VirtualBook::new("alpha", from_int(10_000));

    let first = book
        .apply_fill(&book_fill(
            "buy-1",
            Side::Buy,
            from_int(10),
            from_int(100),
            ONE,
            "AAPL",
        ))
        .unwrap();
    assert_eq!(first.quantity, from_int(10));
    assert_eq!(first.average_cost().unwrap(), from_int(100));
    assert_eq!(first.realized_pnl, -ONE);
    assert_eq!(first.unrealized_pnl().unwrap(), None);
    assert_eq!(book.cash(), from_int(8_999));
    assert_eq!(first.total_pnl().unwrap(), None);

    book.apply_fill(&book_fill(
        "buy-2",
        Side::Buy,
        from_int(5),
        from_int(110),
        500_000_000_000_000_000,
        "AAPL",
    ))
    .unwrap();
    let partial = book
        .apply_fill(&book_fill(
            "sell-part",
            Side::Sell,
            from_int(6),
            from_int(120),
            250_000_000_000_000_000,
            "AAPL",
        ))
        .unwrap();
    assert_eq!(partial.quantity, from_int(9));
    assert_eq!(partial.cost_basis, from_int(930));
    assert_eq!(partial.average_cost().unwrap(), 103_333_333_333_333_333_333);
    assert_eq!(partial.realized_pnl, 98_250_000_000_000_000_000);

    let reversed = book
        .apply_fill(&book_fill(
            "sell-through",
            Side::Sell,
            from_int(12),
            from_int(90),
            750_000_000_000_000_000,
            "AAPL",
        ))
        .unwrap();
    assert_eq!(reversed.quantity, -from_int(3));
    assert_eq!(reversed.cost_basis, -from_int(270));
    assert_eq!(reversed.average_cost().unwrap(), from_int(90));
    assert_eq!(reversed.realized_pnl, -22_500_000_000_000_000_000);
    assert_eq!(book.cash(), 10_247_500_000_000_000_000_000);

    let marker = book.mark("AAPL", from_int(85)).unwrap();
    let marked = book.position("AAPL").unwrap();
    assert_eq!(marker.price, from_int(85));
    assert_eq!(marked.mark_price, Some(from_int(85)));
    assert_eq!(marked.unrealized_pnl().unwrap(), Some(from_int(15)));
    assert_eq!(
        marked.total_pnl().unwrap(),
        Some(-7_500_000_000_000_000_000)
    );

    let snap = book.snapshot();
    assert_eq!(snap.equity().unwrap(), Some(9_992_500_000_000_000_000_000));
    assert_eq!(snap.total_pnl().unwrap(), Some(-7_500_000_000_000_000_000));
    let unrealized = snap.unrealized_pnl().unwrap().unwrap();
    assert_eq!(unrealized, from_int(15));
    assert_eq!(
        snap.total_pnl().unwrap(),
        Some(crate::decimal::add(snap.realized_pnl().unwrap(), unrealized).unwrap())
    );
}

#[test]
fn flat_position_retains_realized_pnl_and_needs_no_marker() {
    let mut book = VirtualBook::new("round-trip", from_int(1_000));
    book.apply_fill(&book_fill(
        "buy",
        Side::Buy,
        from_int(2),
        from_int(10),
        0,
        "AAPL",
    ))
    .unwrap();
    book.apply_fill(&book_fill(
        "buy-recurring-average",
        Side::Buy,
        ONE,
        from_int(11),
        0,
        "AAPL",
    ))
    .unwrap();
    book.apply_fill(&book_fill(
        "partial-close",
        Side::Sell,
        ONE,
        from_int(12),
        0,
        "AAPL",
    ))
    .unwrap();
    let closed = book
        .apply_fill(&book_fill(
            "sell",
            Side::Sell,
            from_int(2),
            from_int(10),
            100_000_000_000_000_000,
            "AAPL",
        ))
        .unwrap();

    assert!(closed.is_flat());
    assert_eq!(closed.average_cost().unwrap(), 0);
    assert_eq!(closed.cost_basis, 0);
    assert_eq!(closed.realized_pnl, 900_000_000_000_000_000);
    assert_eq!(closed.market_value().unwrap(), Some(0));
    assert_eq!(closed.unrealized_pnl().unwrap(), Some(0));
    assert_eq!(book.cash(), 1_000_900_000_000_000_000_000);

    let snap = book.snapshot();
    assert_eq!(snap.equity().unwrap(), Some(1_000_900_000_000_000_000_000));
    assert_eq!(snap.total_pnl().unwrap(), Some(900_000_000_000_000_000));
}

#[test]
fn duplicate_fill_and_invalid_marker_leave_book_unchanged() {
    let mut book = VirtualBook::new("alpha", from_int(100));
    let applied = book_fill("one", Side::Buy, ONE, from_int(10), 0, "AAPL");
    book.apply_fill(&applied).unwrap();

    assert_eq!(book.apply_fill(&applied), Err(super::Error::DuplicateFill));
    assert_eq!(book.mark("AAPL", 0), Err(super::Error::InvalidArgument));

    assert_eq!(book.cash(), from_int(90));
    assert_eq!(book.fill_count(), 1);
    assert_eq!(book.marker_count(), 0);
}

#[test]
fn retire_position_finalizes_expired_contracts_exactly() {
    let mut book = VirtualBook::new("expiry", from_int(1_000));
    book.apply_fill(&book_fill(
        "buy-option",
        Side::Buy,
        ONE,
        from_int(200),
        0,
        "AAPL240119P00100000",
    ))
    .unwrap();
    let worthless = book.retire_position("AAPL240119P00100000", 0).unwrap();

    assert!(worthless.is_flat());
    assert_eq!(worthless.cost_basis, 0);
    assert_eq!(worthless.realized_pnl, -200_000_000_000_000_000_000);
    assert_eq!(worthless.market_value().unwrap(), Some(0));
    assert_eq!(book.cash(), from_int(800));

    let snap = book.snapshot();
    assert_eq!(snap.equity().unwrap(), Some(from_int(800)));
    assert_eq!(
        snap.total_pnl().unwrap(),
        Some(-200_000_000_000_000_000_000)
    );

    let mut assigned = VirtualBook::new("assignment", from_int(1_000));
    assigned
        .apply_fill(&book_fill(
            "sell-option",
            Side::Sell,
            ONE,
            from_int(100),
            0,
            "AAPL240119P00100000",
        ))
        .unwrap();
    let settled = assigned
        .retire_position("AAPL240119P00100000", -from_int(250))
        .unwrap();
    assert_eq!(settled.realized_pnl, -150_000_000_000_000_000_000);
    assert_eq!(assigned.cash(), from_int(850));

    assert_eq!(
        book.retire_position("AAPL240119P00100000", 0),
        Err(super::Error::PositionFlat)
    );
    assert_eq!(
        book.retire_position("AAPL", 0),
        Err(super::Error::UnknownPosition)
    );
}

#[test]
fn snapshot_positions_are_ordered_by_canonical_symbol() {
    let mut book = VirtualBook::new("ordered", from_int(1_000));
    book.apply_fill(&book_fill("msft", Side::Buy, ONE, from_int(10), 0, "MSFT"))
        .unwrap();
    book.apply_fill(&book_fill("aapl", Side::Buy, ONE, from_int(10), 0, "AAPL"))
        .unwrap();
    let snap = book.snapshot();
    assert_eq!(snap.positions.len(), 2);
    assert_eq!(snap.positions[0].symbol, "AAPL");
    assert_eq!(snap.positions[1].symbol, "MSFT");
    assert_eq!(snap.fill_count, 2);
}

#[test]
fn ten_thousand_generated_fill_sequences_conserve_cash_and_position() {
    let initial_cash = from_int(100_000);
    for case_index in 0..10_000usize {
        let mut book = VirtualBook::new("generated", initial_cash);
        let mut expected_cash: i128 = initial_cash;
        let mut expected_quantity: i128 = 0;

        for step in 0..5usize {
            let buy = ((case_index >> step) + step) % 2 == 0;
            let side = if buy { Side::Buy } else { Side::Sell };
            let quantity: i128 = ((case_index * (step + 3) + step) % 7 + 1) as i128 * ONE;
            let price: i128 = (9000 + (case_index * 37 + step * 101) % 3001) as i128 * ONE / 100;
            let fee: i128 = (((case_index + step * 3) % 7) as i128 - 2) * ONE / 100;
            let signed: i128 = if buy { quantity } else { -quantity };
            expected_cash -= mul_div(signed, price, ONE).unwrap() + fee;
            expected_quantity += signed;

            let fill_id = format!("{case_index}-{step}");
            book.apply_fill(&Fill {
                fill_id: &fill_id,
                order_id: "order-generated",
                symbol: "AAPL",
                side,
                quantity,
                price,
                fee,
            })
            .unwrap();
        }

        let mark: i128 = (9500 + case_index % 2001) as i128 * ONE / 100;
        book.mark("AAPL", mark).unwrap();
        let position = book.position("AAPL").unwrap();
        let snap = book.snapshot();

        assert_eq!(expected_cash, book.cash());
        assert_eq!(expected_quantity, position.quantity);
        let expected_market_value = mul_div(expected_quantity, mark, ONE).unwrap();
        assert_eq!(snap.market_value().unwrap(), Some(expected_market_value));
        assert_eq!(
            snap.equity().unwrap(),
            Some(book.cash() + expected_market_value)
        );
        assert_eq!(
            snap.total_pnl().unwrap(),
            Some(
                crate::decimal::add(
                    snap.realized_pnl().unwrap(),
                    snap.unrealized_pnl().unwrap().unwrap()
                )
                .unwrap()
            )
        );
        assert_eq!(snap.fill_count, 5);
    }
}
