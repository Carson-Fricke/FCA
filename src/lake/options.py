"""OPRA option market-data client facade."""

from lake.ingestion.options.backfill import (
    OptionBackfillItem,
    OptionBackfillPlan,
    OptionSpotProvider,
    enqueue_option_backfill,
    plan_option_backfill,
)
from lake.ingestion.options.transaction import (
    OptionIngestResult,
    OptionIngestTransaction,
    OptionMarketDataClient,
)
from lake.ingestion.options.worker import (
    OptionQueueHandler,
    OptionWorker,
    create_option_worker,
)
from lake.providers.alpaca import (
    AlpacaClient,
    NormalizedOptionQuote,
    NormalizedOptionTrade,
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
