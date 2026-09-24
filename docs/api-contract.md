# Truffle API contract (frozen)

Base: `http://localhost:8000`. All JSON. Dates are `YYYY-MM-DD` (UTC).

## Metric keys (stable identifiers)

| key | meaning | unit | nullable |
|---|---|---|---|
| `volume_reputable` | 30d trading volume on green-trust-score exchanges | USD | yes |
| `tvl` | DefiLlama total value locked | USD | yes (no protocol) |
| `exchange_count` | number of exchanges listing the coin | count | yes (`prior` is null until ~30 days of our own history exist -- see `pending`) |
| `rel_btc` | price performance relative to BTC over period | ratio, 0.0 = flat vs BTC | yes |

**Changed 2026-09-24 (breaking).** `commits` and `contributors` are withdrawn: they are no
longer collected, no longer keys of `metrics`, and no longer accepted as a `sort` value.
Runs scored before this date still hold their rows in the database, but the API does not
serve them. Only `volume_reputable` and `rel_btc` carry a weight in the composite; `tvl` and
`exchange_count` are still collected and served, they just do not contribute to `score`.

## MetricValue object
```json
{ "current": 1234.5, "prior": 1000.0, "change_pct": 23.45, "rank_score": 87.3 }
```
`current` = trailing 30d, `prior` = the 30d before that. Any field may be `null`.
`rank_score` is the 0-100 percentile rank of this coin's momentum for that metric within the run's universe.

## CoinRow object
```json
{
  "coin_id": "solana",
  "symbol": "SOL",
  "name": "Solana",
  "image": "https://...",
  "market_cap_rank": 6,
  "market_cap": 91234567890.0,
  "price_usd": 187.44,
  "total_volume": 3456789012.0,
  "score": 71.2,
  "score_raw": 73.8,
  "score_prev": 64.8,
  "score_change": 6.4,
  "missing": ["tvl"],
  "pending": [],
  "metrics": { "volume_reputable": {MetricValue}, "tvl": {...},
               "exchange_count": {...}, "rel_btc": {...} }
}
```
`missing` lists the **weighted** metric keys that did not contribute to `score` for this
coin/run. An unweighted metric (`tvl`, `exchange_count`) never appears there: it could not
have contributed. Rows written by older runs carry that run's `missing` list verbatim, so
they may name keys that are no longer weighted or no longer exist.

`score` is the composite after **variance shrinkage**: the weighted mean of the present `rank_score`s,
recentred on 50 by `f = sqrt(sum q^2)/sqrt(sum p^2)` (`q` = weights over all metrics, `p` = over the present
ones, `f <= 1` and exactly 1 at full coverage). Averaging fewer percentiles widens the spread, so without this
a coin scored on one metric outranks a fully measured one on noise alone. `f` is a function of *which* metrics
are present only, so it never reorders two coins with the same `missing` set. Sorting, `/api/truffles` and
`score_prev`/`score_change` all use this shrunk `score`.

`score_raw` is the same weighted mean *before* shrinkage — exposed for debugging, not for ranking. It is `null`
for runs scored before this field existed, and equals `score` when `scoring.variance_shrinkage` is off.

`pending` is the subset of `missing` we have a `current` reading for but no `prior` window yet -- a metric
whose prior comes from our own stored history is pending until a run ~30 days older exists.
Everything else in `missing` is genuinely unavailable (no DefiLlama protocol, API failure).
Both states are excluded from the score's renormalised weights alike: `pending` says *why* a metric is absent,
never that it counted. Since 2026-09-24 the only such metric, `exchange_count`, is unweighted and so never
reaches `missing`: `pending` is therefore `[]` on new runs. The field and its semantics are unchanged and it
repopulates the moment a history-backed metric is given a weight.

`score_prev`/`score_change` are `null` until a run exists on a strictly earlier `run_date` (same-day re-runs are not treated as history).

## Endpoints

### `GET /api/health`
`{ "status": "ok", "db": "truffle.db", "runs": 12 }`

### `GET /api/runs`
`{ "runs": [ { "run_date": "2026-09-23", "started_at": "...Z", "finished_at": "...Z", "coin_count": 300 } ] }` newest first.

### `GET /api/coins`
Query: `date` (default latest run), `min_volume` (float, filters on `total_volume`), `sort` (any of `score|score_change|market_cap|market_cap_rank|volume_reputable|tvl|exchange_count|rel_btc`, default `score`), `order` (`asc|desc`, default `desc`), `limit` (default 300), `offset`, `q` (name/symbol substring).
```json
{ "run_date": "2026-09-23", "prev_run_date": "2026-09-22", "total": 300, "coins": [CoinRow, ...] }
```
Nulls always sort last. On an empty database this is `200` with `run_date: null`, `prev_run_date: null`, `total: 0` and `coins: []` (same for `/api/truffles`).

### `GET /api/coins/{coin_id}`
Query: `date` (default latest).
```json
{
  "coin": { "coin_id": "solana", "symbol": "SOL", "name": "Solana", "image": "...",
            "categories": ["Smart Contract Platform"], "github_repos": ["solana-labs/solana"],
            "defillama_slug": "solana", "homepage": "https://solana.com",
            "metadata_updated_at": "2026-09-01T00:00:00Z" },
  "latest": CoinRow,
  "history": [ { "run_date": "2026-09-01", "score": 61.0, "score_change": 1.2,
                 "metrics": { "<key>": MetricValue, ... } } ]
}
```
`history` ascending by date and truncated at `date`, so an older run never shows later points. 404 if unknown coin.

`github_repos` is unchanged and still served. It is identity metadata parsed out of the
CoinGecko `/coins/{id}` response we already fetch (repo links for the coin page) and has
never been a metric; withdrawing `commits`/`contributors` does not affect it.

### `GET /api/truffles`
Query: `date`, `limit` (default 10), `min_volume`.
`{ "run_date": "...", "prev_run_date": "...", "truffles": [CoinRow, ...] }` sorted by `score_change` desc. Risers only: coins with a `null` or non-positive `score_change` are excluded, and `truffles` is `[]` whenever `prev_run_date` is `null` (no earlier run date to compare against).

### Errors
`{ "detail": "message" }` with 400/404. An empty database is not an error: the list endpoints answer `200` with an empty list and a `null` `run_date`.
