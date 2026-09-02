"""Public facade for split harness domain records and codecs."""

from harness.core.types.accounting import (
    AccountSnapshot,
    Book,
    BookLevel,
    ExecutionReport,
    Fill,
    Position,
)
from harness.core.types.codecs import (
    EventParquetCodec,
    ParquetCodec,
    SymbolParquetCodec,
    from_parquet,
    symbols_from_parquet,
    symbols_to_parquet,
    to_parquet,
)
from harness.core.types.common import SCHEMA_VERSION, DecimalInput, exact_decimal
from harness.core.types.events import Event, EventType, QuoteEvent, TradeEvent
from harness.core.types.orders import (
    ExecutionType,
    MultiLegOrder,
    Order,
    OrderLeg,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.core.types.schemas import (
    ACCOUNT_SCHEMA,
    BOOK_SCHEMA,
    EVENT_SCHEMAS,
    FILL_SCHEMA,
    MULTI_LEG_ORDER_SCHEMA,
    ORDER_SCHEMA,
    PARQUET_SCHEMAS,
    POSITION_SCHEMA,
    QUOTE_SCHEMA,
    REPORT_SCHEMA,
    SCHEMAS,
    SYMBOL_SCHEMA,
    TRADE_SCHEMA,
)

# Common feed and accounting vocabulary aliases point at the same immutable class.
Quote = QuoteEvent
Trade = TradeEvent
MarketQuote = QuoteEvent
MarketTrade = TradeEvent
OrderReport = ExecutionReport
PositionSnapshot = Position
MarketBook = Book
OrderBook = Book
Level = BookLevel
Report = ExecutionReport


__all__ = [
    "ACCOUNT_SCHEMA",
    "AccountSnapshot",
    "BOOK_SCHEMA",
    "Book",
    "BookLevel",
    "DecimalInput",
    "EVENT_SCHEMAS",
    "Event",
    "EventParquetCodec",
    "EventType",
    "ExecutionReport",
    "ExecutionType",
    "FILL_SCHEMA",
    "Fill",
    "Level",
    "MarketBook",
    "MarketQuote",
    "MarketTrade",
    "MULTI_LEG_ORDER_SCHEMA",
    "MultiLegOrder",
    "ORDER_SCHEMA",
    "Order",
    "OrderBook",
    "OrderLeg",
    "OrderReport",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "PARQUET_SCHEMAS",
    "ParquetCodec",
    "POSITION_SCHEMA",
    "Position",
    "PositionSnapshot",
    "REPORT_SCHEMA",
    "Report",
    "QUOTE_SCHEMA",
    "Quote",
    "QuoteEvent",
    "SCHEMAS",
    "SCHEMA_VERSION",
    "SYMBOL_SCHEMA",
    "Side",
    "SingleLegOrder",
    "SymbolParquetCodec",
    "TRADE_SCHEMA",
    "TimeInForce",
    "Trade",
    "TradeEvent",
    "exact_decimal",
    "from_parquet",
    "symbols_from_parquet",
    "symbols_to_parquet",
    "to_parquet",
]
