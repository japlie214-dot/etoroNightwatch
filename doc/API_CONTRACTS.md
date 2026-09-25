# eToro payload contracts

`nightwatch.etoro.contracts` keeps small, stdlib-only structural contracts for
response fields the client parses. A contract is a set of required or optional
JSON paths, expected primitive/container types, and a few explicit shape rules
(known enums, positive IDs, non-empty identifiers). `$` is the response root;
`[*]` checks every row in an array. Unknown keys are accepted so additive API
fields do not page the operator.

The registry in `PARSER_CONTRACTS` maps the response parsers/helpers in
`etoro.client` and `compute_available_cash` to a contract. The AST test requires
every current parser/endpoint method to appear there, so adding one requires
declaring its observed or explicitly unobserved contract. Each check returns a
report with `missing`, `wrong_type`, or `unexpected_shape` findings. It can emit
one alert-ready summary through `on_violation`; the summary contains no payload
values or account identifiers. `EtoroClient.lookup_order` reports drift at
critical severity, then its existing strict parser rejects missing or invalid
order/position identity; `get_rates` treats quote drift as advisory and
continues. Pass the existing `TelegramNotifier` as
`contract_notifier` to alert on drift. It deduplicates the same environment,
contract, and violation signature for that client instance. Added response
fields remain ignored. With Telegram disabled or credentials absent, the
notifier records a skipped alert and the client keeps the same severity rules.

## Golden evidence

Fourteen compact, sanitized fixtures in `tests/fixtures/etoro_contracts/` retain
one representative response shape from Sep 11 captures:

| Fixtures | Captured response |
|---|---|
| `balances_{real,demo}.json` | `/balances` |
| `portfolio_{real,demo}.json` | portfolio envelope |
| `pnl_{real,demo}.json` | PnL envelope |
| `candles.json`, `rates.json`, `eligibility.json`, `instruments.json`, `costs.json` | market/trading reads |
| `trade_history.json` | one raw closed-trade row |
| `orders_lookup.json` | one generic `orders:lookup` fill |
| `close_response.json` | close POST response |

Account, position, instrument, and order IDs are replaced with local sample IDs;
monetary values and timestamps are synthetic. The close response token and
capture metadata are omitted. These fixtures preserve field names, nesting,
types, and array/object shape, not the operator's account state.

## Known evidence gaps

No Sep 11 raw `/market-data/search` body was captured, all captured portfolio
`orders`/`ordersForOpen` arrays were empty, no rejected or partial-fill lookup
body was captured, and there is no response body for the dedicated GET
`close-orders/{orderId}` endpoint. Those contracts are marked unobserved or
partial; `ContractReport.complete` is false for them. T7's live captures should
close these gaps before stronger rules are added. Only the `Filled` lookup
status label appears in the golden body; the accepted status-name enum mirrors
the client's current status mapping and other labels still lack live fixtures.

The structural checker detects actual response-shape problems whether they
arose later or were missed in an earlier schema review. It does not, by itself,
prove a parser reads the declared path. The golden parser regression asserts
the observed order status, nested position ID, fill units/price, and nested close
order ID reach the parsed result.

The checker alone would **not** have caught the four 09-11 parser mistakes
while their raw payloads remained shape-valid: reading `status` as a scalar,
looking for `positionId` at the order root, reading nonexistent `units` instead
of `requestedUnits`/execution units, and looking for the close `orderID` only at
the root. The parser-to-golden regression catches those mapping errors; a
missing field or type change is caught by the structural checker and its
alert-ready report.
