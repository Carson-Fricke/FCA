from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import ExecutionReport, Fill
from harness.core.types.orders import (
    ExecutionType,
    MultiLegOrder,
    OrderLeg,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.venues.alpaca import (
    AlpacaBrokerHTTPError,
    AlpacaClientOrderIdSource,
    AlpacaCommandReceipt,
    AlpacaVenueError,
    AlpacaVenueProtocolError,
    UnknownAlpacaOrderError,
)
from harness.venues.alpaca_options import (
    CAPABILITY_MATRIX_VERSION,
    AlpacaCapabilityMatrix,
    AlpacaOptionsBroker,
    AlpacaOptionsBrokerRest,
    AlpacaOptionsCapabilityMatrix,
    AlpacaOptionsExecutionInbox,
    AlpacaOptionsOrderBinding,
    AlpacaOptionsOrderDirectory,
    AlpacaOptionsVenue,
    CapabilityRejection,
    UnsupportedAlpacaOptionsFeatureError,
    UnsupportedFeatureReason,
    VenueCapabilitySupport,
)
from harness.venues.scripted import VenueOperation

START = datetime(2024, 1, 19, 14, 30, 1, tzinfo=UTC)
BASE_URL = "https://paper-api.alpaca.markets"
LONG_CALL = "SPY240119C00450000"
SHORT_CALL = "SPY240119C00460000"
WING_CALL = "SPY240119C00470000"
AAPL_CALL = "AAPL240119C00150000"
_SPY: Final = Symbol("SPY")


class FakeClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers: Mapping[str, str] = {} if headers is None else headers

    def json(self) -> object:
        return self.payload


class FakeRestTransport:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.requests: list[dict[str, object]] = []
        self.responses = list(responses)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str],
        json: object | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": dict(params),
                "json": json,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def exact(message: dict[str, object]) -> dict[str, object]:
    payload: Any = json.loads(json.dumps(message), parse_float=Decimal)
    assert isinstance(payload, dict)
    return payload


def leg(
    symbol: str,
    side: Side,
    quantity: Decimal = Decimal("1"),
    ratio: Decimal = Decimal("1"),
) -> OrderLeg:
    return OrderLeg(symbol=Symbol(symbol), side=side, quantity=quantity, ratio=ratio)


def vertical(
    order_id: str = "opt-1",
    *,
    first_symbol: str = LONG_CALL,
    second_symbol: str = SHORT_CALL,
    first_side: Side = Side.BUY,
    second_side: Side = Side.SELL,
    quantity: Decimal = Decimal("1"),
    first_ratio: Decimal = Decimal("1"),
    second_ratio: Decimal = Decimal("1"),
    order_type: OrderType = OrderType.MARKET,
    time_in_force: TimeInForce = TimeInForce.DAY,
    limit_price: Decimal | None = None,
    client_order_id: str | None = None,
) -> MultiLegOrder:
    return MultiLegOrder(
        event_id="order-opt-1",
        timestamp=START,
        sequence=0,
        order_id=order_id,
        legs=[
            leg(
                first_symbol,
                first_side,
                quantity=quantity,
                ratio=first_ratio,
            ),
            leg(
                second_symbol,
                second_side,
                quantity=quantity,
                ratio=second_ratio,
            ),
        ],
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )


def single_equity(
    *,
    symbol: Symbol = _SPY,
    order_type: OrderType = OrderType.MARKET,
    stop_price: Decimal | None = None,
) -> SingleLegOrder:
    return SingleLegOrder(
        event_id="order-eq-1",
        timestamp=START,
        sequence=0,
        order_id="eq-1",
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=order_type,
        time_in_force=TimeInForce.DAY,
        stop_price=stop_price,
    )


def options_broker(
    transport: FakeRestTransport,
    *,
    matrix: AlpacaOptionsCapabilityMatrix | None = None,
) -> AlpacaOptionsBroker:
    return AlpacaOptionsBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaOptionsExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
        matrix=matrix,
    )


def deterministic_client_ids(
    strategy_id: str = "mom-1",
) -> AlpacaClientOrderIdSource:
    counter = iter(range(10_000))

    def entropy(width: int) -> int:
        return next(counter)

    return AlpacaClientOrderIdSource(
        strategy_id=strategy_id,
        time_ms=lambda: 1705684200000,
        entropy=entropy,
    )


def accepted_response() -> FakeResponse:
    return FakeResponse(200, {"id": "venue-opt-1", "status": "new"})


def group_order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "id": "venue-opt-1",
        "client_order_id": "cid-opt-1",
        "symbol": "SPY",
        "qty": "1",
        "filled_qty": "0",
        "filled_avg_price": None,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "status": "new",
        "updated_at": "2024-01-19T14:30:00.400000Z",
    }
    order.update(overrides)
    return order


def legged_order(**overrides: object) -> dict[str, object]:
    order = group_order(**overrides)
    order["legs"] = [
        {
            "symbol": LONG_CALL,
            "side": "buy",
            "qty": "1",
            "ratio_qty": 1,
            "filled_qty": "0",
            "status": "new",
        },
        {
            "symbol": SHORT_CALL,
            "side": "sell",
            "qty": "1",
            "ratio_qty": 1,
            "filled_qty": "0",
            "status": "new",
        },
    ]
    return order


def leg_execution(
    symbol: str = LONG_CALL,
    execution_id: str = "exec-leg-1",
    qty: str = "1",
    price: str = "4.50",
) -> dict[str, object]:
    return {
        "id": execution_id,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "timestamp": "2024-01-19T14:30:00.450000Z",
    }


def trade_update(
    event: str,
    order: dict[str, object],
    *,
    at: str = "2024-01-19T14:30:00.500000Z",
    execution: dict[str, object] | None = None,
    stream: str = "trade_updates",
) -> dict[str, object]:
    data: dict[str, object] = {"at": at, "event": event, "order": order}
    if execution is not None:
        data["execution"] = execution
    return {"stream": stream, "data": data}


def bound_directory() -> AlpacaOptionsOrderDirectory:
    directory = AlpacaOptionsOrderDirectory()
    directory.bind(
        AlpacaOptionsOrderBinding(
            order_id="opt-1",
            client_order_id="cid-opt-1",
            spread_count=Decimal("1"),
            legs=(
                leg(LONG_CALL, Side.BUY),
                leg(SHORT_CALL, Side.SELL),
            ),
        )
    )
    return directory


def bound_inbox(**kwargs: Any) -> AlpacaOptionsExecutionInbox:
    return AlpacaOptionsExecutionInbox(directory=bound_directory(), **kwargs)


def test_options_matrix_accepts_a_valid_vertical() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    assert matrix.check(vertical()) == ()
    assert matrix.version == CAPABILITY_MATRIX_VERSION


def test_options_matrix_rejects_unsupported_order_type() -> None:
    matrix = AlpacaOptionsCapabilityMatrix(
        version="test", order_types=frozenset({OrderType.MARKET})
    )

    rejections = matrix.check(
        vertical(order_type=OrderType.LIMIT, limit_price=Decimal("1.50"))
    )

    assert [reason.reason for reason in rejections] == [
        UnsupportedFeatureReason.ORDER_TYPE
    ]


def test_options_matrix_rejects_ioc_time_in_force() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    rejections = matrix.check(vertical(time_in_force=TimeInForce.IOC))

    assert [reason.reason for reason in rejections] == [
        UnsupportedFeatureReason.TIME_IN_FORCE
    ]


def test_options_matrix_rejects_leg_count_violations() -> None:
    matrix = AlpacaOptionsCapabilityMatrix(version="test", max_legs=2)
    three_legs = MultiLegOrder(
        event_id="order-opt-3",
        timestamp=START,
        sequence=0,
        order_id="opt-3",
        legs=[
            leg(LONG_CALL, Side.BUY),
            leg(SHORT_CALL, Side.SELL),
            leg(WING_CALL, Side.BUY),
        ],
    )

    rejections = matrix.check(three_legs)

    assert [reason.reason for reason in rejections] == [
        UnsupportedFeatureReason.LEG_COUNT
    ]


def test_options_matrix_rejects_non_osi_and_mixed_underlying_legs() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    equity_legs = MultiLegOrder(
        event_id="order-opt-4",
        timestamp=START,
        sequence=0,
        order_id="opt-4",
        legs=[leg("SPY", Side.BUY), leg("QQQ", Side.SELL)],
    )
    rejections = matrix.check(equity_legs)
    assert rejections[0].reason is UnsupportedFeatureReason.LEG_STRUCTURE
    assert "OSI" in rejections[0].detail

    mixed = MultiLegOrder(
        event_id="order-opt-5",
        timestamp=START,
        sequence=0,
        order_id="opt-5",
        legs=[leg(LONG_CALL, Side.BUY), leg(AAPL_CALL, Side.SELL)],
    )
    rejections = matrix.check(mixed)
    assert rejections[0].reason is UnsupportedFeatureReason.LEG_STRUCTURE
    assert "one underlying" in rejections[0].detail


def test_options_matrix_rejects_duplicate_contracts() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()
    duplicated = MultiLegOrder(
        event_id="order-opt-6",
        timestamp=START,
        sequence=0,
        order_id="opt-6",
        legs=[leg(LONG_CALL, Side.BUY), leg(LONG_CALL, Side.SELL)],
    )

    rejections = matrix.check(duplicated)

    assert rejections[0].reason is UnsupportedFeatureReason.LEG_STRUCTURE
    assert "more than once" in rejections[0].detail


def test_options_matrix_rejects_fractional_ratios() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    rejections = matrix.check(
        vertical(
            first_ratio=Decimal("1.5"),
            second_ratio=Decimal("1.5"),
        )
    )

    assert rejections[0].reason is UnsupportedFeatureReason.LEG_RATIO


def test_options_matrix_rejects_fractional_spread_counts() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    rejections = matrix.check(vertical(quantity=Decimal("3"), first_ratio=Decimal("2")))

    assert rejections[0].reason is UnsupportedFeatureReason.QUANTITY


def test_options_matrix_rejects_inconsistent_spread_counts() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()
    mixed = MultiLegOrder(
        event_id="order-opt-7",
        timestamp=START,
        sequence=0,
        order_id="opt-7",
        legs=[
            leg(LONG_CALL, Side.BUY, quantity=Decimal("2")),
            leg(SHORT_CALL, Side.SELL),
        ],
    )

    rejections = matrix.check(mixed)

    assert rejections[0].reason is UnsupportedFeatureReason.LEG_STRUCTURE
    assert "common spread count" in rejections[0].detail


def test_options_matrix_rejects_extended_hours_features() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    rejections = matrix.check(vertical(), {"extended_hours": True})

    assert rejections[0].reason is UnsupportedFeatureReason.EXTENDED_HOURS


def test_options_matrix_rejects_bracket_oto_oco_order_classes() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    for order_class in ("bracket", "oto", "oco"):
        rejections = matrix.check(vertical(), {"order_class": order_class})
        assert rejections[0].reason is UnsupportedFeatureReason.ORDER_CLASS
        assert order_class in rejections[0].detail
    assert matrix.check(vertical(), {"order_class": "mleg"}) == ()


def test_options_matrix_rejects_attachment_features() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    for key in ("take_profit", "stop_loss", "trailing_stop", "trail_price"):
        rejections = matrix.check(vertical(), {key: Decimal("1")})
        assert rejections[0].reason is UnsupportedFeatureReason.ORDER_CLASS


def test_options_matrix_rejects_unknown_features() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    rejections = matrix.check(vertical(), {"not_a_feature": 1})

    assert rejections[0].reason is UnsupportedFeatureReason.UNKNOWN_FEATURE


def test_options_matrix_collects_every_rejection_at_once() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    rejections = matrix.check(
        vertical(time_in_force=TimeInForce.IOC),
        {"extended_hours": True, "take_profit": Decimal("1"), "wat": 2},
    )

    assert [reason.reason for reason in rejections] == [
        UnsupportedFeatureReason.EXTENDED_HOURS,
        UnsupportedFeatureReason.ORDER_CLASS,
        UnsupportedFeatureReason.UNKNOWN_FEATURE,
        UnsupportedFeatureReason.TIME_IN_FORCE,
    ]


def test_options_matrix_require_raises_with_full_rejection_set() -> None:
    matrix = AlpacaOptionsCapabilityMatrix.alpaca_options_default()

    with pytest.raises(UnsupportedAlpacaOptionsFeatureError) as excinfo:
        matrix.require(vertical(time_in_force=TimeInForce.IOC))
    error = excinfo.value
    assert error.reason is UnsupportedFeatureReason.TIME_IN_FORCE
    assert error.feature == "time_in_force"
    assert len(error.rejections) == 1
    assert "[time_in_force]" in str(error)


def test_options_matrix_boundaries_are_validated() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        AlpacaOptionsCapabilityMatrix(version="test", min_legs=1)
    with pytest.raises(ValueError, match="below min_legs"):
        AlpacaOptionsCapabilityMatrix(version="test", min_legs=3, max_legs=2)


def test_submit_maps_market_vertical_to_mleg_payload() -> None:
    transport = FakeRestTransport([accepted_response()])
    inbox = AlpacaOptionsExecutionInbox()
    venue = AlpacaOptionsBroker(
        api_key="key",
        api_secret="secret",
        inbox=inbox,
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )

    receipt = run(venue.submit(vertical()))

    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == f"{BASE_URL}/v2/orders"
    assert request["headers"] == {
        "APCA-API-KEY-ID": "key",
        "APCA-API-SECRET-KEY": "secret",
        "Accept": "application/json",
    }
    body = request["json"]
    assert isinstance(body, dict)
    assert body["qty"] == "1"
    assert body["side"] == "buy"
    assert body["type"] == "market"
    assert body["time_in_force"] == "day"
    assert body["order_class"] == "mleg"
    assert "limit_price" not in body
    assert body["legs"] == [
        {
            "symbol": LONG_CALL,
            "ratio_qty": 1,
            "side": "buy",
            "position_intent": "buy_to_open",
        },
        {
            "symbol": SHORT_CALL,
            "ratio_qty": 1,
            "side": "sell",
            "position_intent": "sell_to_open",
        },
    ]
    client_order_id = body["client_order_id"]
    assert client_order_id.startswith("mom-1-")
    assert len(client_order_id) <= 48
    assert receipt == AlpacaCommandReceipt(
        VenueOperation.SUBMIT, order_id="opt-1", client_order_id=client_order_id
    )
    assert inbox.directory.client_order_id_for("opt-1") == client_order_id


def test_submit_maps_net_debit_limit_to_positive_wire_price() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = options_broker(transport)
    order = vertical(
        order_type=OrderType.LIMIT,
        limit_price=Decimal("1.50"),
        first_side=Side.BUY,
        second_side=Side.SELL,
    )

    run(venue.submit(order))

    body = transport.requests[0]["json"]
    assert isinstance(body, dict)
    assert body["limit_price"] == "1.50"


def test_submit_maps_net_credit_limit_to_negative_wire_price() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = options_broker(transport)
    order = vertical(
        order_type=OrderType.LIMIT,
        limit_price=Decimal("1.50"),
        first_side=Side.SELL,
        second_side=Side.BUY,
    )

    run(venue.submit(order))

    body = transport.requests[0]["json"]
    assert isinstance(body, dict)
    assert body["side"] == "sell"
    assert body["limit_price"] == "-1.50"
    assert body["legs"][0]["position_intent"] == "sell_to_open"
    assert body["legs"][1]["position_intent"] == "buy_to_open"


def test_submit_maps_ratio_spreads_to_integral_ratio_qty() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = options_broker(transport)
    order = MultiLegOrder(
        event_id="order-opt-ratio",
        timestamp=START,
        sequence=0,
        order_id="opt-ratio",
        legs=[
            leg(
                LONG_CALL,
                Side.BUY,
                quantity=Decimal("2"),
                ratio=Decimal("2"),
            ),
            leg(SHORT_CALL, Side.SELL),
        ],
    )

    run(venue.submit(order))

    body = transport.requests[0]["json"]
    assert isinstance(body, dict)
    assert body["qty"] == "1"
    assert body["legs"][0]["ratio_qty"] == 2
    assert body["legs"][1]["ratio_qty"] == 1
    assert "limit_price" not in body


def test_submit_honors_explicit_client_order_id() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = options_broker(transport)

    receipt = run(venue.submit(vertical(client_order_id="replay-opt-1")))

    body = transport.requests[0]["json"]
    assert isinstance(body, dict)
    assert body["client_order_id"] == "replay-opt-1"
    assert receipt.client_order_id == "replay-opt-1"


def test_submit_rejects_unsupported_combinations_before_transport() -> None:
    transport = FakeRestTransport([])
    venue = options_broker(transport)

    with pytest.raises(UnsupportedAlpacaOptionsFeatureError):
        run(venue.submit(vertical(), features={"extended_hours": True}))
    with pytest.raises(UnsupportedAlpacaOptionsFeatureError):
        run(venue.submit(vertical(time_in_force=TimeInForce.IOC)))
    with pytest.raises(UnsupportedAlpacaOptionsFeatureError):
        run(venue.submit(vertical(quantity=Decimal("3"), first_ratio=Decimal("2"))))
    with pytest.raises(TypeError, match="MultiLegOrder"):
        run(venue.submit(single_equity()))
    assert not transport.requests


def test_replace_is_rejected_explicitly() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = options_broker(transport)
    run(venue.submit(vertical()))

    with pytest.raises(
        UnsupportedAlpacaOptionsFeatureError, match="modify multi-leg"
    ) as excinfo:
        run(venue.replace("opt-1", vertical(order_id="opt-1r")))

    assert excinfo.value.reason is UnsupportedFeatureReason.ORDER_MODIFICATION
    assert len(transport.requests) == 1


def test_cancel_maps_to_delete_by_bound_client_order_id() -> None:
    transport = FakeRestTransport([accepted_response(), FakeResponse(204, None)])
    venue = options_broker(transport)
    receipt = run(venue.submit(vertical()))
    assert receipt.client_order_id is not None

    cancel_receipt = run(venue.cancel("opt-1"))

    request = transport.requests[1]
    assert request["method"] == "DELETE"
    assert request["url"] == f"{BASE_URL}/v2/orders/{receipt.client_order_id}"
    assert cancel_receipt == AlpacaCommandReceipt(
        VenueOperation.CANCEL,
        order_id="opt-1",
        client_order_id=receipt.client_order_id,
    )


def test_cancel_unknown_order_raises() -> None:
    venue = options_broker(FakeRestTransport([]))

    with pytest.raises(UnknownAlpacaOrderError, match="opt-9"):
        run(venue.cancel("opt-9"))


def test_commands_retry_rate_limits_with_retry_after() -> None:
    transport = FakeRestTransport(
        [FakeResponse(429, None, {"retry-after": "2"}), accepted_response()]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    venue = AlpacaOptionsBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaOptionsExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
        sleep=sleep,
    )

    run(venue.submit(vertical()))

    assert sleeps == [2.0]
    assert len(transport.requests) == 2


def test_commands_raise_after_retry_exhaustion() -> None:
    transport = FakeRestTransport([FakeResponse(500, None), FakeResponse(500, None)])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    venue = AlpacaOptionsBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaOptionsExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        max_attempts=2,
        sleep=sleep,
    )

    with pytest.raises(AlpacaBrokerHTTPError, match="HTTP 500"):
        run(venue.submit(vertical()))

    assert sleeps == [0.25]
    assert len(transport.requests) == 2


def test_options_rest_client_validates_construction() -> None:
    with pytest.raises(ValueError, match="api_key"):
        AlpacaOptionsBrokerRest(api_key=" ", api_secret="secret")
    with pytest.raises(ValueError, match="max_attempts"):
        AlpacaOptionsBrokerRest(api_key="k", api_secret="s", max_attempts=0)


def test_venue_alias_and_no_state_introspection() -> None:
    venue = options_broker(FakeRestTransport([]))

    assert AlpacaOptionsVenue is AlpacaOptionsBroker
    for attribute in (
        "state",
        "order_state",
        "orders",
        "active_orders",
        "decisions",
        "request_position_snapshot",
    ):
        assert not hasattr(venue, attribute)


def test_group_ack_becomes_kernel_report_in_spread_counts() -> None:
    inbox = bound_inbox()

    events = inbox.push(exact(trade_update("new", group_order())), observed_at=START)

    assert len(events) == 1
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.order_id == "opt-1"
    assert report.status is OrderStatus.NEW
    assert report.execution_type is ExecutionType.ACKNOWLEDGED
    assert report.order_quantity == Decimal("1")
    assert report.filled_quantity == Decimal("0")
    assert report.remaining_quantity == Decimal("1")
    assert report.text == "alpaca:new"
    assert report.event_id.startswith("alpaca-options-broker:report:cid-opt-1:")
    assert [event.sequence for event in events] == [0]


def test_group_rejection_and_cancellation_map_statuses() -> None:
    inbox = bound_inbox()

    rejected = inbox.push(
        exact(trade_update("rejected", group_order(status="rejected"))),
        observed_at=START,
    )
    canceled = inbox.push(
        exact(trade_update("canceled", group_order(status="canceled"))),
        observed_at=START,
    )

    assert isinstance(rejected[0], ExecutionReport)
    assert rejected[0].status is OrderStatus.REJECTED
    assert rejected[0].text == "alpaca:rejected"
    assert isinstance(canceled[0], ExecutionReport)
    assert canceled[0].status is OrderStatus.CANCELED
    assert canceled[0].text == "alpaca:canceled"


def test_one_lot_spy_vertical_updates_each_virtual_book_leg_exactly() -> None:
    inbox = bound_inbox()

    first = inbox.push(
        exact(
            trade_update(
                "fill",
                legged_order(),
                execution=leg_execution(
                    symbol=LONG_CALL, execution_id="exec-leg-1", price="4.50"
                ),
            )
        ),
        observed_at=START,
    )
    second = inbox.push(
        exact(
            trade_update(
                "fill",
                legged_order(),
                execution=leg_execution(
                    symbol=SHORT_CALL,
                    execution_id="exec-leg-2",
                    qty="1",
                    price="1.20",
                ),
            )
        ),
        observed_at=START,
    )
    events = (*first, *second)

    assert [event.sequence for event in events] == [0, 1, 2, 3]
    long_report = events[0]
    assert isinstance(long_report, ExecutionReport)
    assert long_report.order_id == "opt-1"
    assert long_report.status is OrderStatus.FILLED
    assert long_report.order_quantity == Decimal("1")
    assert long_report.filled_quantity == Decimal("1")
    assert long_report.remaining_quantity == Decimal("0")
    assert long_report.last_fill_price == Decimal("4.50")
    assert long_report.text == f"alpaca:fill:{LONG_CALL}"
    long_fill = events[1]
    assert isinstance(long_fill, Fill)
    assert long_fill.order_id == "opt-1"
    assert long_fill.symbol == LONG_CALL
    assert long_fill.side is Side.BUY
    assert long_fill.quantity == Decimal("1")
    assert long_fill.price == Decimal("4.50")
    short_report = events[2]
    assert isinstance(short_report, ExecutionReport)
    assert short_report.text == f"alpaca:fill:{SHORT_CALL}"
    short_fill = events[3]
    assert isinstance(short_fill, Fill)
    assert short_fill.symbol == SHORT_CALL
    assert short_fill.side is Side.SELL
    assert short_fill.price == Decimal("1.20")
    assert short_fill.event_id.startswith("alpaca-options-broker:fill:exec-leg-2:")
    assert inbox.drain() == tuple(events)
    assert len(inbox) == 0


def test_leg_partial_fill_maps_partially_filled_report_and_fill() -> None:
    inbox = bound_inbox()

    events = inbox.push(
        exact(
            trade_update(
                "partial_fill",
                legged_order(),
                execution=leg_execution(qty="0.4", price="4.48"),
            )
        ),
        observed_at=START,
    )

    assert [event.sequence for event in events] == [0, 1]
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.status is OrderStatus.PARTIALLY_FILLED
    assert report.execution_type is ExecutionType.PARTIAL_FILL
    assert report.filled_quantity == Decimal("0.4")
    assert report.remaining_quantity == Decimal("0.6")
    fill = events[1]
    assert isinstance(fill, Fill)
    assert fill.quantity == Decimal("0.4")
    assert fill.price == Decimal("4.48")


def test_leg_fill_exceeding_leg_quantity_is_rejected_atomically() -> None:
    inbox = bound_inbox(initial_sequence=5)

    with pytest.raises(AlpacaVenueProtocolError, match="exceeds the"):
        inbox.push(
            exact(
                trade_update(
                    "fill",
                    legged_order(),
                    execution=leg_execution(qty="2"),
                )
            ),
            observed_at=START,
        )

    assert len(inbox) == 0
    assert inbox.next_sequence == 5


def test_leg_fill_for_unknown_contract_is_rejected() -> None:
    inbox = bound_inbox()

    with pytest.raises(AlpacaVenueProtocolError, match="not a leg"):
        inbox.push(
            exact(
                trade_update(
                    "fill",
                    legged_order(),
                    execution=leg_execution(symbol=AAPL_CALL),
                )
            ),
            observed_at=START,
        )
    assert len(inbox) == 0


def test_group_fill_without_execution_is_rejected() -> None:
    inbox = bound_inbox()

    with pytest.raises(AlpacaVenueProtocolError, match="requires an execution"):
        inbox.push(
            exact(trade_update("fill", group_order(filled_qty="1"))),
            observed_at=START,
        )
    assert len(inbox) == 0


def test_net_credit_group_average_maps_to_none_documented() -> None:
    inbox = bound_inbox()

    debit = inbox.push(
        exact(
            trade_update(
                "calculated",
                group_order(filled_qty="1", filled_avg_price="4.10"),
            )
        ),
        observed_at=START,
    )
    credit = inbox.push(
        exact(
            trade_update(
                "calculated",
                group_order(filled_qty="1", filled_avg_price="-0.85"),
            )
        ),
        observed_at=START,
    )

    debit_report = debit[0]
    assert isinstance(debit_report, ExecutionReport)
    assert debit_report.average_fill_price == Decimal("4.10")
    credit_report = credit[0]
    assert isinstance(credit_report, ExecutionReport)
    assert credit_report.average_fill_price is None
    assert credit_report.status is OrderStatus.FILLED


def test_unbound_group_surfaces_wire_client_order_id() -> None:
    inbox = AlpacaOptionsExecutionInbox()

    events = inbox.push(
        exact(trade_update("new", group_order(client_order_id="ext-opt-9"))),
        observed_at=START,
    )

    assert len(events) == 1
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.order_id == "ext-opt-9"


def test_unbound_leg_frame_uses_frame_legs_for_selection() -> None:
    inbox = AlpacaOptionsExecutionInbox()

    events = inbox.push(
        exact(
            trade_update(
                "fill",
                legged_order(client_order_id="ext-opt-9", filled_qty="1"),
                execution=leg_execution(symbol=SHORT_CALL, price="1.20"),
            )
        ),
        observed_at=START,
    )

    assert [event.sequence for event in events] == [0, 1]
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.order_id == "ext-opt-9"
    assert report.text == f"alpaca:fill:{SHORT_CALL}"
    fill = events[1]
    assert isinstance(fill, Fill)
    assert fill.order_id == "ext-opt-9"
    assert fill.side is Side.SELL


def test_bound_frame_legs_must_match_the_binding() -> None:
    inbox = bound_inbox()
    frame = trade_update("new", group_order())
    data = frame["data"]
    assert isinstance(data, dict)
    data["order"]["legs"] = [
        {"symbol": LONG_CALL, "side": "buy", "qty": "1", "ratio_qty": 1}
    ]

    with pytest.raises(AlpacaVenueProtocolError, match="bound group"):
        inbox.push(exact(frame), observed_at=START)
    assert len(inbox) == 0


def test_stream_timestamp_sanity_is_explicit_and_atomic() -> None:
    inbox = bound_inbox(
        initial_sequence=7,
        max_future_skew=timedelta(seconds=1),
        max_event_age=timedelta(seconds=10),
    )

    with pytest.raises(AlpacaVenueProtocolError, match="future skew"):
        inbox.push(
            exact(trade_update("new", group_order(), at="2024-01-19T14:30:03Z")),
            observed_at=START,
        )
    with pytest.raises(AlpacaVenueProtocolError, match="maximum event age"):
        inbox.push(
            exact(trade_update("new", group_order(), at="2024-01-19T14:29:50Z")),
            observed_at=START,
        )
    with pytest.raises(AlpacaVenueProtocolError, match="usable ISO timestamp"):
        frame = trade_update("new", group_order())
        data = frame["data"]
        assert isinstance(data, dict)
        del data["at"]
        del data["order"]["updated_at"]
        inbox.push(exact(frame), observed_at=START)

    assert len(inbox) == 0
    assert inbox.next_sequence == 7
    accepted = inbox.push(exact(trade_update("new", group_order())), observed_at=START)
    assert [event.sequence for event in accepted] == [7]


def test_bad_stream_frames_raise_protocol_errors() -> None:
    inbox = bound_inbox()

    with pytest.raises(
        AlpacaVenueProtocolError, match="unsupported Alpaca broker trade update"
    ):
        inbox.push(exact(trade_update("surprise", group_order())), observed_at=START)
    with pytest.raises(AlpacaVenueProtocolError, match="stream"):
        inbox.push(
            exact(
                {
                    "stream": "authorization",
                    "data": {"action": "authenticate", "status": "authorized"},
                }
            ),
            observed_at=START,
        )
    assert len(inbox) == 0


def test_leg_fills_dispatch_through_the_kernel_event_bus() -> None:
    inbox = bound_inbox()
    for symbol, price in ((LONG_CALL, "4.50"), (SHORT_CALL, "1.20")):
        inbox.push(
            exact(
                trade_update(
                    "fill",
                    legged_order(),
                    execution=leg_execution(symbol=symbol, price=price),
                )
            ),
            observed_at=START,
        )
    events = inbox.drain()

    bus = EventBus(SimClock(datetime(2024, 1, 19, 14, 29, 0, tzinfo=UTC)))
    for event in events:
        bus.schedule(event)
    dispatched = bus.drain()
    assert [type(event) for event in dispatched] == [
        ExecutionReport,
        Fill,
        ExecutionReport,
        Fill,
    ]
    fills = [event for event in dispatched if isinstance(event, Fill)]
    assert [str(fill.symbol) for fill in fills] == [LONG_CALL, SHORT_CALL]
    assert [fill.sequence for fill in fills] == [1, 3]


def test_consolidated_matrix_routes_multi_leg_orders_to_options() -> None:
    matrix = AlpacaCapabilityMatrix.alpaca_default()

    rejections = matrix.check(vertical(time_in_force=TimeInForce.IOC))
    assert rejections[0].reason is UnsupportedFeatureReason.TIME_IN_FORCE
    assert matrix.check(vertical()) == ()


def test_consolidated_matrix_rejects_single_leg_option_orders() -> None:
    matrix = AlpacaCapabilityMatrix.alpaca_default()
    single_option = SingleLegOrder(
        event_id="order-eq-2",
        timestamp=START,
        sequence=0,
        order_id="eq-2",
        symbol=Symbol(LONG_CALL),
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("4.50"),
    )

    rejections = matrix.check(single_option)

    assert rejections[0].reason is UnsupportedFeatureReason.LEG_COUNT


def test_consolidated_matrix_routes_crypto_pairs_to_the_crypto_venue() -> None:
    matrix = AlpacaCapabilityMatrix.alpaca_default()

    rejections = matrix.check(
        single_equity(
            symbol=Symbol("BTC/USD"),
            order_type=OrderType.STOP,
            stop_price=Decimal("50000"),
        )
    )
    assert [reason.reason for reason in rejections] == [
        UnsupportedFeatureReason.ASSET_CLASS,
        UnsupportedFeatureReason.ORDER_TYPE,
    ]
    assert matrix.check(single_equity(symbol=Symbol("BTC/USD"))) == (rejections[:1])


def test_consolidated_matrix_rejects_equity_extended_features() -> None:
    matrix = AlpacaCapabilityMatrix.alpaca_default()

    extended = matrix.check(single_equity(), {"extended_hours": True})
    assert extended[0].reason is UnsupportedFeatureReason.EXTENDED_HOURS
    bracket = matrix.check(single_equity(), {"take_profit": Decimal("1")})
    assert bracket[0].reason is UnsupportedFeatureReason.ORDER_CLASS
    mleg = matrix.check(single_equity(), {"order_class": "mleg"})
    assert mleg[0].reason is UnsupportedFeatureReason.ORDER_CLASS
    unknown = matrix.check(single_equity(), {"wat": 1})
    assert unknown[0].reason is UnsupportedFeatureReason.UNKNOWN_FEATURE
    assert matrix.check(single_equity(), {"order_class": "simple"}) == ()
    assert matrix.check(single_equity()) == ()


def test_consolidated_matrix_require_raises_and_documents_rejections() -> None:
    matrix = AlpacaCapabilityMatrix.alpaca_default()

    with pytest.raises(UnsupportedAlpacaOptionsFeatureError):
        matrix.require(
            single_equity(
                symbol=Symbol("BTC/USD"),
                order_type=OrderType.STOP,
                stop_price=Decimal("50000"),
            )
        )


def test_consolidated_matrix_rows_cover_all_three_venues() -> None:
    matrix = AlpacaCapabilityMatrix.alpaca_default()

    capabilities = matrix.to_capabilities()
    features = {capability.feature for capability in capabilities}
    assert {
        "equity_single_leg_submit",
        "equity_extended_hours",
        "crypto_fractional_submit",
        "crypto_position_snapshot",
        "options_multi_leg_submit",
        "options_replace",
        "options_position_snapshot",
    } <= features
    rejected = {
        capability.feature
        for capability in capabilities
        if capability.support is VenueCapabilitySupport.REJECTED
    }
    assert {
        "equity_extended_hours",
        "equity_brackets",
        "crypto_extended_hours",
        "options_time_in_force_ioc",
        "options_replace",
        "options_brackets",
    } <= rejected


def test_capability_document_matches_the_rendered_matrix() -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "harness"
        / "venues"
        / "CAPABILITY_MATRIX.md"
    )
    matrix = AlpacaCapabilityMatrix.alpaca_default()

    document = path.read_text(encoding="utf-8")
    assert document == matrix.to_markdown()
    assert f"Version: {CAPABILITY_MATRIX_VERSION}" in document


def test_capability_rejections_and_errors_validate_inputs() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        CapabilityRejection(UnsupportedFeatureReason.LEG_COUNT, " ", "detail")
    with pytest.raises(ValueError, match="rejections"):
        UnsupportedAlpacaOptionsFeatureError(())
    rejection = CapabilityRejection(
        UnsupportedFeatureReason.LEG_COUNT, "legs", "detail"
    )
    assert UnsupportedFeatureReason(rejection.reason) is rejection.reason
    with pytest.raises(AlpacaVenueError):
        raise UnsupportedAlpacaOptionsFeatureError((rejection,))


def test_directory_binds_idempotently_and_rejects_conflicts() -> None:
    directory = AlpacaOptionsOrderDirectory()
    binding = AlpacaOptionsOrderBinding(
        order_id="opt-1",
        client_order_id="cid-opt-1",
        spread_count=Decimal("1"),
        legs=(leg(LONG_CALL, Side.BUY), leg(SHORT_CALL, Side.SELL)),
    )

    directory.bind(binding)
    directory.bind(binding)
    assert directory.binding_for_client("cid-opt-1") == binding
    assert directory.client_order_id_for("opt-1") == "cid-opt-1"
    with pytest.raises(AlpacaVenueError, match="already bound"):
        directory.bind(
            AlpacaOptionsOrderBinding(
                order_id="opt-2",
                client_order_id="cid-opt-1",
                spread_count=Decimal("1"),
                legs=(leg(LONG_CALL, Side.BUY), leg(SHORT_CALL, Side.SELL)),
            )
        )
    with pytest.raises(TypeError, match="binding"):
        directory.bind(cast("AlpacaOptionsOrderBinding", object()))
