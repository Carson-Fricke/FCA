"""OPRA option market-data client facade."""

from lake.alpaca import (
    AlpacaClient,
    NormalizedOptionQuote,
    NormalizedOptionTrade,
)
from lake.option_backfill import (
    OptionBackfillItem,
    OptionBackfillPlan,
    OptionSpotProvider,
    enqueue_option_backfill,
    plan_option_backfill,
)
from lake.option_ingest import (
    OptionIngestResult,
    OptionIngestTransaction,
    OptionMarketDataClient,
    OptionQueueHandler,
    OptionWorker,
    create_option_worker,
)


class AlpacaOptionsClient(AlpacaClient):
    """Fetch historical OPRA data for selected T024 option contracts."""


OptionsClient = AlpacaOptionsClient
OptionTrade = NormalizedOptionTrade
OptionQuote = NormalizedOptionQuote


__all__ = [
    "AlpacaOptionsClient",
    "NormalizedOptionQuote",
    "NormalizedOptionTrade",
    "OptionBackfillItem",
    "OptionBackfillPlan",
    "OptionIngestResult",
    "OptionIngestTransaction",
    "OptionMarketDataClient",
    "OptionQuote",
    "OptionQueueHandler",
    "OptionSpotProvider",
    "OptionTrade",
    "OptionWorker",
    "OptionsClient",
    "create_option_worker",
    "enqueue_option_backfill",
    "plan_option_backfill",
]
