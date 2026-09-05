# Alpaca venue capability matrix

Version: 2026-09.1

Consolidated capability policy for the Alpaca venue family: the
live equity broker (`harness.venues.alpaca`), the deterministic
crypto venue (`harness.venues.alpaca_crypto`), and the live
options multi-leg broker (`harness.venues.alpaca_options`).
Unsupported combinations are rejected explicitly with
`UnsupportedAlpacaOptionsFeatureError` before broker submission;
nothing degrades silently. This document is rendered from
`AlpacaCapabilityMatrix.to_markdown()` and must carry the
`CAPABILITY_MATRIX_VERSION` 2026-09.1.

| Feature | Support | Detail |
| --- | --- | --- |
| equity_single_leg_submit | supported | market, limit, stop, and stop-limit orders on one equity symbol |
| equity_time_in_force | supported | day, gtc, and ioc time in force |
| equity_cancel | supported | resolved through the order directory to the bound client order id |
| equity_replace | supported | quantity, time in force, and prices; symbol, side, and order type are fixed by Alpaca replace |
| equity_position_snapshot | supported | GET /v2/positions rows become kernel Position events |
| equity_multi_leg | rejected | multi-leg orders submit through the options adapter only |
| equity_option_contracts | rejected | option contracts submit through the options adapter as multi-leg spreads |
| equity_crypto_pairs | rejected | crypto pairs route to the deterministic AlpacaCryptoVenue |
| equity_extended_hours | rejected | extended-hours flags are rejected before broker submission |
| equity_brackets | rejected | bracket, oto, oco, take-profit, and stop-loss attachments are rejected before broker submission |
| crypto_fractional_submit | supported | fractional market and limit orders on BASE/QUOTE pairs, continuous across every UTC day |
| crypto_replace | supported | atomic replace with symbol and side fixed |
| crypto_fee_reconciliation | supported | every fill reconciles the local maker-taker model against the venue fee schedule |
| crypto_extended_hours | rejected | crypto trades continuously; extended-hours flags do not apply |
| crypto_position_snapshot | rejected | positions belong to the accounting hub; the venue validates the protocol command only |
| options_multi_leg_submit | supported | mleg orders with 2 to 4 legs on one underlying |
| options_order_types | supported | limit, market orders; net-debit limits map to a positive wire limit_price and net-credit limits to a negative wire limit_price |
| options_time_in_force | supported | day, gtc |
| options_time_in_force_ioc | rejected | ioc is rejected before broker submission |
| options_cancel | supported | group-level cancel through the bound parent client order id |
| options_replace | rejected | Alpaca cannot modify multi-leg orders; cancel and resubmit instead |
| options_extended_hours | rejected | extended-hours flags are rejected before broker submission |
| options_brackets | rejected | bracket, oto, oco, take-profit, and stop-loss attachments are rejected before broker submission |
| options_position_snapshot | supported | served by the shared AlpacaBroker equity positions path |
