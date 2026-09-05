from __future__ import annotations

import asyncio
import json
import uuid as uuid_module
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import ParquetCodec
from harness.core.types.accounting import ExecutionReport, Fill, Position
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
    AlpacaBroker,
    AlpacaBrokerHTTPError,
    AlpacaClientOrderIdSource,
    AlpacaCommandReceipt,
    AlpacaExecutionInbox,
    AlpacaOrderBinding,
    AlpacaOrderDirectory,
    AlpacaVenueError,
    AlpacaVenueProtocolError,
    UnknownAlpacaOrderError,
)
from harness.venues.scripted import VenueOperation

START = datetime(2024, 1, 19, 14, 30, 1, tzinfo=UTC)
BASE_URL = "https://paper-api.alpaca.markets"


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


def limit_order(
    order_id: str = "ord-1",
    symbol: str = "SPY",
    side: Side = Side.BUY,
    quantity: Decimal = Decimal("3"),
    limit_price: Decimal = Decimal("482.10"),
    client_order_id: str | None = None,
) -> SingleLegOrder:
    return SingleLegOrder(
        event_id="order-1",
        timestamp=START,
        sequence=0,
        order_id=order_id,
        symbol=Symbol(symbol),
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )


def market_order(
    order_id: str = "ord-1",
    quantity: Decimal = Decimal("1"),
    client_order_id: str | None = None,
) -> SingleLegOrder:
    return SingleLegOrder(
        event_id="order-1",
        timestamp=START,
        sequence=0,
        order_id=order_id,
        symbol=Symbol("SPY"),
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )


def accepted_response() -> FakeResponse:
    return FakeResponse(200, {"id": "venue-1", "status": "new"})


def broker_order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "id": "venue-1",
        "client_order_id": "cid-1",
        "symbol": "SPY",
        "qty": "10",
        "filled_qty": "0",
        "filled_avg_price": None,
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "status": "new",
        "updated_at": "2024-01-19T14:30:00.400000Z",
    }
    order.update(overrides)
    return order


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


def execution(
    execution_id: str = "exec-1",
    qty: str = "4",
    price: str = "482.05",
) -> dict[str, object]:
    return {
        "id": execution_id,
        "price": price,
        "qty": qty,
        "timestamp": "2024-01-19T14:30:00.450000Z",
        "position_qty": "4",
    }


def bound_directory() -> AlpacaOrderDirectory:
    directory = AlpacaOrderDirectory()
    directory.bind(
        AlpacaOrderBinding(
            order_id="ord-1",
            client_order_id="cid-1",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.LIMIT,
        )
    )
    return directory


def bound_inbox(**kwargs: Any) -> AlpacaExecutionInbox:
    return AlpacaExecutionInbox(directory=bound_directory(), **kwargs)


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


def test_uuid7_client_ids_are_strategy_tagged_and_rfc9562_shaped() -> None:
    source = deterministic_client_ids()
    identifier = source.next_client_order_id()
    assert identifier.startswith("mom-1-")
    assert len(identifier) <= 48
    raw = identifier[len("mom-1-") :]
    parsed = uuid_module.UUID(raw)
    assert parsed.version == 7
    assert (parsed.int >> 62) & 0b11 == 0b10
    twin = deterministic_client_ids()
    assert twin.next_client_order_id() == identifier
    assert source.next_client_order_id() != identifier


def test_strategy_tags_are_sanitized_and_bounded() -> None:
    source = AlpacaClientOrderIdSource(
        strategy_id="SPX Strat!!",
        time_ms=lambda: 0,
        entropy=lambda width: 0,
    )
    assert source.strategy_tag == "SPX-Strat"
    assert len(source.next_client_order_id()) <= 48
    fallback = AlpacaClientOrderIdSource(
        strategy_id="!!!",
        time_ms=lambda: 0,
        entropy=lambda width: 0,
    )
    assert fallback.strategy_tag == "unattribute"


def test_submit_maps_to_rest_and_returns_receipt() -> None:
    transport = FakeRestTransport([accepted_response()])
    inbox = AlpacaExecutionInbox()
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=inbox,
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )

    receipt = run(venue.submit(market_order()))

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
    assert body["symbol"] == "SPY"
    assert body["qty"] == "1"
    assert body["side"] == "buy"
    assert body["type"] == "market"
    assert body["time_in_force"] == "day"
    client_order_id = body["client_order_id"]
    assert client_order_id.startswith("mom-1-")
    assert len(client_order_id) <= 48
    assert receipt == AlpacaCommandReceipt(
        VenueOperation.SUBMIT,
        order_id="ord-1",
        client_order_id=client_order_id,
    )
    assert inbox.directory.client_order_id_for("ord-1") == client_order_id


def test_submit_payload_carries_exact_limit_and_stop_prices() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )
    order = SingleLegOrder(
        event_id="order-1",
        timestamp=START,
        sequence=0,
        order_id="ord-2",
        symbol=Symbol("AAPL"),
        side=Side.SELL,
        quantity=Decimal("2.5"),
        order_type=OrderType.STOP_LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price=Decimal("190.25"),
        stop_price=Decimal("189.50"),
    )

    run(venue.submit(order))

    body = transport.requests[0]["json"]
    assert isinstance(body, dict)
    assert body["symbol"] == "AAPL"
    assert body["qty"] == "2.5"
    assert body["side"] == "sell"
    assert body["type"] == "stop_limit"
    assert body["time_in_force"] == "gtc"
    assert body["limit_price"] == "190.25"
    assert body["stop_price"] == "189.50"
    assert body["client_order_id"].startswith("mom-1-")


def test_submit_honors_explicit_client_order_id() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )

    receipt = run(venue.submit(market_order(client_order_id="replay-1")))

    body = transport.requests[0]["json"]
    assert isinstance(body, dict)
    assert body["client_order_id"] == "replay-1"
    assert receipt.client_order_id == "replay-1"


def test_submit_rejects_multi_leg_options_and_crypto() -> None:
    transport = FakeRestTransport([])
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
    )
    multi = MultiLegOrder(
        event_id="order-3",
        timestamp=START,
        sequence=0,
        order_id="ord-3",
        legs=[
            OrderLeg(symbol=Symbol("SPY"), side=Side.BUY, quantity=Decimal("1")),
            OrderLeg(symbol=Symbol("QQQ"), side=Side.SELL, quantity=Decimal("1")),
        ],
    )
    with pytest.raises(AlpacaVenueError, match="single-leg"):
        run(venue.submit(multi))
    with pytest.raises(AlpacaVenueError, match="option"):
        run(venue.submit(limit_order(symbol="AAPL240119C00150000")))
    with pytest.raises(AlpacaVenueError, match="crypto"):
        run(venue.submit(limit_order(symbol="BTC/USD")))
    assert not transport.requests


def test_cancel_maps_to_delete_by_bound_client_order_id() -> None:
    transport = FakeRestTransport([accepted_response(), FakeResponse(204, None)])
    inbox = AlpacaExecutionInbox()
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=inbox,
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )
    run(venue.submit(market_order()))
    client_order_id = inbox.directory.client_order_id_for("ord-1")
    assert client_order_id is not None

    receipt = run(venue.cancel("ord-1"))

    request = transport.requests[1]
    assert request["method"] == "DELETE"
    assert request["url"] == f"{BASE_URL}/v2/orders/{client_order_id}"
    assert receipt == AlpacaCommandReceipt(
        VenueOperation.CANCEL, order_id="ord-1", client_order_id=client_order_id
    )


def test_cancel_unknown_order_raises() -> None:
    transport = FakeRestTransport([])
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
    )
    with pytest.raises(UnknownAlpacaOrderError, match="ord-9"):
        run(venue.cancel("ord-9"))
    assert not transport.requests


def test_replace_maps_to_patch_and_rebinds_identity() -> None:
    transport = FakeRestTransport([accepted_response(), accepted_response()])
    inbox = AlpacaExecutionInbox()
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=inbox,
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )
    run(venue.submit(limit_order()))
    previous_client = inbox.directory.client_order_id_for("ord-1")
    assert previous_client is not None

    receipt = run(
        venue.replace(
            "ord-1",
            limit_order(
                order_id="ord-1r",
                quantity=Decimal("5"),
                limit_price=Decimal("482.20"),
            ),
        )
    )

    request = transport.requests[1]
    assert request["method"] == "PATCH"
    assert request["url"] == f"{BASE_URL}/v2/orders/{previous_client}"
    body = request["json"]
    assert isinstance(body, dict)
    assert body["qty"] == "5"
    assert body["limit_price"] == "482.20"
    assert body["time_in_force"] == "day"
    new_client = body["client_order_id"]
    assert new_client != previous_client
    assert receipt == AlpacaCommandReceipt(
        VenueOperation.REPLACE, order_id="ord-1r", client_order_id=new_client
    )
    assert inbox.directory.client_order_id_for("ord-1r") == new_client
    chain = inbox.directory.chain_for_client(new_client)
    assert [binding.order_id for binding in chain] == ["ord-1", "ord-1r"]


def test_replace_rejects_symbol_side_type_and_reused_ids() -> None:
    transport = FakeRestTransport([accepted_response()])
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )
    run(venue.submit(limit_order()))
    with pytest.raises(AlpacaVenueError, match="symbol"):
        run(venue.replace("ord-1", limit_order(order_id="ord-x", symbol="AAPL")))
    with pytest.raises(AlpacaVenueError, match="side"):
        run(venue.replace("ord-1", limit_order(order_id="ord-x", side=Side.SELL)))
    with pytest.raises(AlpacaVenueError, match="order type"):
        run(
            venue.replace(
                "ord-1",
                market_order(order_id="ord-x", quantity=Decimal("3")),
            )
        )
    with pytest.raises(AlpacaVenueError, match="new kernel order_id"):
        run(venue.replace("ord-1", limit_order(order_id="ord-1")))
    assert len(transport.requests) == 1


def test_commands_retry_rate_limits_with_retry_after() -> None:
    transport = FakeRestTransport(
        [FakeResponse(429, None, {"retry-after": "2"}), accepted_response()]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
        sleep=sleep,
    )

    receipt = run(venue.submit(market_order()))

    assert sleeps == [2.0]
    assert receipt.order_id == "ord-1"
    assert len(transport.requests) == 2


def test_commands_raise_after_retry_exhaustion() -> None:
    transport = FakeRestTransport([FakeResponse(500, None), FakeResponse(500, None)])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=transport,
        max_attempts=2,
        sleep=sleep,
    )

    with pytest.raises(AlpacaBrokerHTTPError, match="HTTP 500"):
        run(venue.submit(market_order()))

    assert sleeps == [0.25]
    assert len(transport.requests) == 2


def test_ack_stream_becomes_kernel_execution_report() -> None:
    inbox = bound_inbox()

    events = inbox.push(exact(trade_update("new", broker_order())), observed_at=START)

    assert len(events) == 1
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.order_id == "ord-1"
    assert report.status is OrderStatus.NEW
    assert report.execution_type is ExecutionType.ACKNOWLEDGED
    assert report.order_quantity == Decimal("10")
    assert report.filled_quantity == Decimal("0")
    assert report.remaining_quantity == Decimal("10")
    assert report.last_fill_quantity == Decimal("0")
    assert report.last_fill_price is None
    assert report.average_fill_price is None
    assert report.text == "alpaca:new"
    assert report.timestamp == datetime(2024, 1, 19, 14, 30, 0, 500000, tzinfo=UTC)
    assert [event.sequence for event in events] == [0]
    assert report.event_id.startswith("alpaca-broker:report:cid-1:")
    assert report.report_id == report.event_id
    restored = ParquetCodec.decode(ParquetCodec.encode([report]), type(report))[0]
    assert restored == report


def test_partial_fill_stream_yields_report_and_fill() -> None:
    inbox = bound_inbox(initial_sequence=7)

    events = inbox.push(
        exact(
            trade_update(
                "partial_fill",
                broker_order(
                    filled_qty="4",
                    filled_avg_price="482.02",
                    status="partially_filled",
                ),
                execution=execution(),
            )
        ),
        observed_at=START,
    )

    assert [event.sequence for event in events] == [7, 8]
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.status is OrderStatus.PARTIALLY_FILLED
    assert report.execution_type is ExecutionType.PARTIAL_FILL
    assert report.order_quantity == Decimal("10")
    assert report.filled_quantity == Decimal("4")
    assert report.remaining_quantity == Decimal("6")
    assert report.average_fill_price == Decimal("482.02")
    assert report.last_fill_quantity == Decimal("4")
    assert report.last_fill_price == Decimal("482.05")
    fill = events[1]
    assert isinstance(fill, Fill)
    assert fill.order_id == "ord-1"
    assert fill.symbol == "SPY"
    assert fill.side is Side.BUY
    assert fill.quantity == Decimal("4")
    assert fill.price == Decimal("482.05")
    assert fill.fee == Decimal("0")
    assert fill.event_id.startswith("alpaca-broker:fill:exec-1:")
    assert fill.fill_id == fill.event_id


def test_fill_stream_yields_terminal_report_and_fill() -> None:
    inbox = bound_inbox()

    events = inbox.push(
        exact(
            trade_update(
                "fill",
                broker_order(
                    filled_qty="10",
                    filled_avg_price="482.00",
                    status="filled",
                ),
                execution=execution(execution_id="exec-2", qty="10", price="482.00"),
            )
        ),
        observed_at=START,
    )

    assert len(events) == 2
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.status is OrderStatus.FILLED
    assert report.execution_type is ExecutionType.FILL
    assert report.remaining_quantity == Decimal("0")
    fill = events[1]
    assert isinstance(fill, Fill)
    assert fill.quantity == Decimal("10")
    assert fill.price == Decimal("482.00")


def test_rejected_stream_yields_rejection_report() -> None:
    inbox = bound_inbox()

    events = inbox.push(
        exact(trade_update("rejected", broker_order(status="rejected"))),
        observed_at=START,
    )

    assert len(events) == 1
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.status is OrderStatus.REJECTED
    assert report.execution_type is ExecutionType.REJECTED
    assert report.text == "alpaca:rejected"


def test_terminal_streams_map_cancellation_and_expiry() -> None:
    inbox = bound_inbox()

    canceled = inbox.push(
        exact(trade_update("canceled", broker_order(status="canceled"))),
        observed_at=START,
    )
    assert [event.sequence for event in canceled] == [0]
    assert isinstance(canceled[0], ExecutionReport)
    assert canceled[0].status is OrderStatus.CANCELED
    assert canceled[0].execution_type is ExecutionType.CANCELED

    expired = inbox.push(
        exact(trade_update("expired", broker_order(status="expired"))),
        observed_at=START,
    )
    assert isinstance(expired[0], ExecutionReport)
    assert expired[0].status is OrderStatus.EXPIRED

    done_for_day = inbox.push(
        exact(trade_update("done_for_day", broker_order())), observed_at=START
    )
    assert isinstance(done_for_day[0], ExecutionReport)
    assert done_for_day[0].status is OrderStatus.EXPIRED


def test_pending_streams_map_status_reports() -> None:
    inbox = bound_inbox()

    for event_name in ("pending_cancel", "pending_replace"):
        events = inbox.push(
            exact(trade_update(event_name, broker_order())), observed_at=START
        )
        assert len(events) == 1
        report = events[0]
        assert isinstance(report, ExecutionReport)
        assert report.status is OrderStatus.CANCEL_PENDING
        assert report.execution_type is ExecutionType.STATUS


def test_replaced_stream_synthesizes_old_and_new_reports() -> None:
    directory = bound_directory()
    directory.link_replacement(
        "cid-1",
        AlpacaOrderBinding(
            order_id="ord-1r",
            client_order_id="cid-2",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("6"),
            order_type=OrderType.LIMIT,
        ),
    )
    inbox = AlpacaExecutionInbox(directory=directory)

    events = inbox.push(
        exact(
            trade_update(
                "replaced",
                broker_order(
                    client_order_id="cid-2",
                    qty="6",
                    filled_qty="2",
                    filled_avg_price="482.02",
                ),
            )
        ),
        observed_at=START,
    )

    assert len(events) == 2
    replaced = events[0]
    assert isinstance(replaced, ExecutionReport)
    assert replaced.order_id == "ord-1"
    assert replaced.status is OrderStatus.REPLACED
    assert replaced.execution_type is ExecutionType.REPLACED
    assert replaced.order_quantity == Decimal("10")
    assert replaced.filled_quantity == Decimal("2")
    assert replaced.remaining_quantity == Decimal("8")
    acknowledged = events[1]
    assert isinstance(acknowledged, ExecutionReport)
    assert acknowledged.order_id == "ord-1r"
    assert acknowledged.status is OrderStatus.ACCEPTED
    assert acknowledged.execution_type is ExecutionType.ACKNOWLEDGED
    assert acknowledged.order_quantity == Decimal("6")
    assert acknowledged.filled_quantity == Decimal("2")
    assert acknowledged.remaining_quantity == Decimal("4")
    assert [event.sequence for event in events] == [0, 1]


def test_replaced_stream_without_chain_surfaces_under_binding() -> None:
    inbox = bound_inbox()

    events = inbox.push(
        exact(trade_update("replaced", broker_order())), observed_at=START
    )

    assert len(events) == 1
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.order_id == "ord-1"
    assert report.status is OrderStatus.REPLACED
    assert report.execution_type is ExecutionType.REPLACED


def test_unknown_stream_event_raises_protocol_error() -> None:
    inbox = bound_inbox()

    with pytest.raises(
        AlpacaVenueProtocolError, match="unsupported Alpaca broker trade update"
    ):
        inbox.push(exact(trade_update("surprise", broker_order())), observed_at=START)
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
    assert inbox.next_sequence == 0


def test_stream_timestamp_sanity_is_explicit_and_atomic() -> None:
    inbox = bound_inbox(
        initial_sequence=7,
        max_future_skew=timedelta(seconds=1),
        max_event_age=timedelta(seconds=10),
    )

    with pytest.raises(AlpacaVenueProtocolError, match="future skew"):
        inbox.push(
            exact(trade_update("new", broker_order(), at="2024-01-19T14:30:03Z")),
            observed_at=START,
        )
    with pytest.raises(AlpacaVenueProtocolError, match="maximum event age"):
        inbox.push(
            exact(trade_update("new", broker_order(), at="2024-01-19T14:29:50Z")),
            observed_at=START,
        )
    with pytest.raises(AlpacaVenueProtocolError, match="usable ISO timestamp"):
        frame = trade_update("new", broker_order())
        data = frame["data"]
        assert isinstance(data, dict)
        del data["at"]
        del data["order"]["updated_at"]
        inbox.push(exact(frame), observed_at=START)

    assert len(inbox) == 0
    assert inbox.next_sequence == 7
    accepted = inbox.push(exact(trade_update("new", broker_order())), observed_at=START)
    assert [event.sequence for event in accepted] == [7]


def test_unbound_client_order_id_falls_back_to_wire_identity() -> None:
    inbox = AlpacaExecutionInbox()

    events = inbox.push(
        exact(trade_update("new", broker_order(client_order_id="ext-9"))),
        observed_at=START,
    )

    assert len(events) == 1
    report = events[0]
    assert isinstance(report, ExecutionReport)
    assert report.order_id == "ext-9"


def test_position_snapshot_rows_become_position_events() -> None:
    inbox = AlpacaExecutionInbox()

    events = inbox.push_positions(
        [
            {
                "symbol": "SPY",
                "qty": "3",
                "avg_entry_price": "481.50",
                "current_price": "482.10",
                "unrealized_pl": "1.80",
            },
            {
                "symbol": "AAPL",
                "qty": "-2",
                "avg_entry_price": "190.00",
                "current_price": "191.00",
                "unrealized_pl": "-2.00",
            },
        ],
        observed_at=START,
        account_id="paper-acct",
    )

    assert [event.sequence for event in events] == [0, 1]
    spy = events[0]
    assert isinstance(spy, Position)
    assert spy.account_id == "paper-acct"
    assert spy.symbol == "SPY"
    assert spy.quantity == Decimal("3")
    assert spy.average_price == Decimal("481.50")
    assert spy.market_price == Decimal("482.10")
    assert spy.unrealized_pnl == Decimal("1.80")
    aapl = events[1]
    assert isinstance(aapl, Position)
    assert aapl.quantity == Decimal("-2")
    assert aapl.unrealized_pnl == Decimal("-2.00")
    for event in events:
        restored = ParquetCodec.decode(ParquetCodec.encode([event]), type(event))[0]
        assert restored == event


def test_positions_response_must_be_an_array() -> None:
    inbox = AlpacaExecutionInbox()
    with pytest.raises(
        AlpacaVenueProtocolError, match="positions response must be an array"
    ):
        inbox.push_positions(
            {"message": "no positions"},
            observed_at=START,
            account_id="paper-acct",
        )


def test_venue_round_trip_dispatches_through_kernel_event_bus() -> None:
    transport = FakeRestTransport([accepted_response()])
    inbox = AlpacaExecutionInbox()
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=inbox,
        clock=FakeClock(),
        transport=transport,
        client_ids=deterministic_client_ids(),
    )

    receipt = run(venue.submit(market_order()))
    assert receipt.order_id == "ord-1"
    client_order_id = inbox.directory.client_order_id_for("ord-1")
    assert client_order_id is not None

    inbox.push(
        exact(trade_update("new", broker_order(client_order_id=client_order_id))),
        observed_at=START,
    )
    inbox.push(
        exact(
            trade_update(
                "fill",
                broker_order(
                    client_order_id=client_order_id,
                    filled_qty="1",
                    filled_avg_price="482.05",
                    status="filled",
                ),
                execution=execution(execution_id="exec-9", qty="1", price="482.05"),
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
        ExecutionReport,
        Fill,
    ]
    ack = dispatched[0]
    assert isinstance(ack, ExecutionReport)
    assert ack.order_id == "ord-1"
    assert ack.execution_type is ExecutionType.ACKNOWLEDGED
    assert ack.sequence == 0
    filled_report = dispatched[1]
    assert isinstance(filled_report, ExecutionReport)
    assert filled_report.execution_type is ExecutionType.FILL
    assert filled_report.sequence == 1
    fill = dispatched[2]
    assert isinstance(fill, Fill)
    assert fill.order_id == "ord-1"
    assert fill.quantity == Decimal("1")
    assert fill.price == Decimal("482.05")
    assert fill.sequence == 2


def test_venue_exposes_no_state_introspection() -> None:
    venue = AlpacaBroker(
        api_key="key",
        api_secret="secret",
        inbox=AlpacaExecutionInbox(),
        clock=FakeClock(),
        transport=FakeRestTransport([]),
    )

    for attribute in (
        "state",
        "order_state",
        "orders",
        "active_orders",
        "decisions",
        "positions",
    ):
        assert not hasattr(venue, attribute)


def test_receipts_never_carry_venue_outcomes() -> None:
    receipt = AlpacaCommandReceipt(
        VenueOperation.SUBMIT, order_id="ord-1", client_order_id="cid-1"
    )
    assert receipt.operation is VenueOperation.SUBMIT
    assert not hasattr(receipt, "status")
    assert not hasattr(receipt, "filled_quantity")
    assert not hasattr(receipt, "venue_order_id")
    with pytest.raises(ValueError, match="identifiers"):
        AlpacaCommandReceipt(VenueOperation.SUBMIT, order_id="ord-1")
    with pytest.raises(ValueError, match="identifiers"):
        AlpacaCommandReceipt(VenueOperation.POSITION_SNAPSHOT, order_id="ord-1")
