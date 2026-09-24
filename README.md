# Truffle

Truffle screens the top 300 cryptocurrencies by market cap for **rising fundamental
momentum** — metrics that cost real money, real engineering hours or real
third-party cooperation to move, so they are expensive to fake. Every run appends
an immutable slice to SQLite; nothing is overwritten, so past universes and scores
stay intact (no survivorship bias).

## Setup

```bash
git clone <repo> && cd truffle
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit it (see below)
```

| `.env` var | required? | why |
|---|---|---|
| `COINGECKO_API_KEY` | **strongly recommended** | the public tier throttles at ~5 req/min. A free Demo key gives 100/min, capped at **10,000 call credits per calendar month**. |

It runs without it — degrading, recording the failure and marking the affected
metrics missing — but a 300-coin run is not practical that way.

**Attribution is required:** any UI built on this data must display
"Data provided by CoinGecko".

### Run the pipeline

```bash
python run.py --limit 20 -v      # 20-coin smoke run
python run.py                    # full universe (config: universe_size, default 300)
python run.py --dry-run          # universe + call/time projection, writes nothing
```

| flag | effect |
|---|---|
| `--limit N` | universe size override |
| `--refresh` / `--no-cache` | bypass the disk cache (still rewrites it) |
| `--skip-metadata` | never refetch static metadata |
| `--dry-run` | projection only, no writes |
| `--date YYYY-MM-DD` | label the run with a specific date |
| `--ignore-budget` | run even if it would exceed the month's remaining CoinGecko credits |
| `-v` / `--verbose` | per-coin debug lines, including each coin's missing-metric list |

### Collect hourly bars (Binance)

Separate from the scoring run and on its own cadence — see
[Hourly OHLCV ingestion](#hourly-ohlcv-ingestion).

```bash
python collect.py --map          # build/refresh the Binance symbol -> coin_id map
python collect.py --backfill     # one-time 90-day history (binance.backfill_days)
python collect.py                # incremental: every bar since the last one stored
```

### Serve the API

```bash
uvicorn api.main:app --reload --port 8000
curl localhost:8000/api/health
```

CORS is open for `http://localhost:5173`. The frozen contract is in
[`docs/api-contract.md`](docs/api-contract.md). One thing it leaves open:
`sort=<metric key>` sorts on that metric's `rank_score` (its momentum percentile),
not its raw value; `score`, `score_change`, `market_cap` and `market_cap_rank`
sort on their own values.

### Frontend

```bash
cd frontend && npm install && npm run dev      # http://localhost:5173
```

### Tests

`make test`, or `python -m pytest -q`.

The dashboard only ever shows what the database holds. Metric history accumulates
one run at a time, so the charts stay flat until several runs exist; if the API is
not running, the UI says so rather than showing anything else.

## The metrics

All four compare the trailing 30 days against the prior 30 (`window_days`). Collection
and scoring are separate concerns: every metric below is collected and stored on every
run, but only the two with a weight in `config.yaml` feed the composite.

| metric | weighted? | what it measures | why it is hard to fake |
|---|---|---|---|
| `volume_reputable` | **0.20** | 30d volume scaled by the share of ticker volume on exchanges CoinGecko scores green (`trust_score >= 7`) | wash trading is cheap only on unranked venues; tier-1 venues mean real spread, real fees and audited order books |
| `rel_btc` | **0.15** | coin 30d return − BTC 30d return (`0.0` = flat vs BTC) | the one deliberately market-based signal; subtracting BTC removes beta. The most gameable of the four, so it carries the smaller weight |
| `tvl` | no | mean DefiLlama TVL per window, matched on `gecko_id`; `null` where the coin has no protocol | faking it means actually locking capital in public contracts, at real opportunity cost; DefiLlama strips most double-counting |
| `exchange_count` | no | distinct green-trust-score exchanges listing the coin | listings are gated by third parties who run legal and technical review and charge for it |

`tvl` and `exchange_count` are stored but unweighted. They are kept collecting because a
value/fundamental factor (TVL ÷ market cap) is an untested direction, and because
`exchange_count`'s prior comes from our own history — deleting the collector would restart
that clock at zero. The collectors are cheap; the history is not reproducible.

GitHub activity (`commits`, `contributors`) was collected until 2026-09-24 and has been
removed: the backtests found no edge in the composite it fed, and it is not a direction
worth the API budget. The historical rows stay in `metrics`; nothing reads them.

## Scoring

1. **Momentum** — `log((current + 1) / (prior + 1))`: symmetric for doubling vs
   halving, scale-free, finite at a zero prior. `rel_btc` is already signed, so its
   momentum is a plain difference.
2. **Rank transform** — percentile-rank each metric's momentum to `0-100` within
   that run's universe (`rank_score`), ties sharing the mean rank.
3. **Weighted average** — the weighted mean of the `rank_score`s actually present,
   weights (from `config.yaml`) renormalised over them. A missing metric is
   dropped, never zero-filled — but dropping it is not free, see the next step.
   This is stored as `score_raw`.
4. **Variance shrinkage** — a weighted average of fewer roughly independent
   percentiles has a *wider* spread, so a plain renormalised average lets sparsely
   measured coins monopolise both ends of the leaderboard (on the 300-coin run 10,
   1-2 metric coins reached 99.9 while the best-measured coins topped out at 63.8).
   The centred score is therefore rescaled to the full-coverage spread:

   ```
   f = sqrt(sum q_i^2) / sqrt(sum p_i^2)      # q = weights over ALL weighted metrics, p = over the present ones
   score = 50 + (score_raw - 50) * f          # f <= 1, exactly 1 at full coverage
   ```

   `f` depends only on *which* metrics are present, never on their values, so
   ordering inside a coverage bucket is untouched; only the spread between buckets is
   equalised. Over the current two weighted metrics `f` is two-valued: exactly `1` with
   both present and exactly `5/7` (the weights are 4:3) with either one alone, so a
   half-measured coin keeps ~71% of its distance from 50. It still binds:
   `volume_reputable` needs tickers and `rel_btc` needs a price chart, and on the last
   300-coin run 20 coins had exactly one of the two.
   `score` is the shrunk number — it is what
   `/api/coins` ranks on — and `score_raw` is kept beside it for debugging. Set
   `scoring.variance_shrinkage: false` in `config.yaml` for the plain average
   (`score == score_raw`).
5. **Score change** — `score − score_prev` from the latest run with a **strictly
   earlier `run_date`**. Same-day re-runs are not history: a score is a percentile
   rank inside its own run's universe, so it is only comparable across dates.
   `null` when no earlier date exists; `/api/truffles` then returns nothing. Both
   sides are shrunk scores, so the difference is comparable.

## Operational notes

- **Universe.** One `/coins/markets` call over-fetches ~1.6x; exclusions apply and
  the top `universe_size` survive. Every ranked coin is stored, kept or excluded,
  with the reason. Exclusion order: CoinGecko category (authoritative), symbol
  blocklist, name substrings, wrapper-prefix rule (`WETH → ETH` excludes,
  `WAVES → AVES` does not), peg-price heuristic. Lists live in `config.yaml`.
- **No prior for `exchange_count`** (nor the reputable-volume share) — CoinGecko
  gives them point-in-time only, so the prior comes from our own storage ~30 days
  back. Until the DB holds 30 days of real runs `exchange_count` has a `current` but
  no `prior`, so it has no momentum and no `rank_score`. Since it also carries no
  weight it cannot reach the score by any route, so nothing is currently `pending`.
  The `pending` machinery stays in place and stays tested: give any history-backed
  metric a weight and it is listed under `missing` *and* under `pending` (a metric we
  can read but cannot score yet, as opposed to one we never collected) while its weight
  drops out of `weight_coverage` rather than being counted as present.
  The share is not scored itself: it scales the volume windows, and the prior window
  falls back to today's share until its own history exists. It is stored as
  `_reputable_share`; `_`-prefixed keys never reach the API.
- **Storage.** SQLite at `data/truffle.db`, plain `sqlite3`, idempotent migrations
  in `truffle/db.py`. Runs append only. Raw responses cache to
  `data/cache/<source>/` for `cache.ttl_seconds` (24h).
- **Rate limits.** Per-host `rate_per_min` in `config.yaml`, jittered backoff,
  `Retry-After`, `X-RateLimit-Reset`. A host whose quota resets later than
  `http.max_pause_seconds` is disabled for the rest of the run rather than stalling
  it; its metrics come back `null`. Shipped rates assume both keys are set.
- **Monthly credit budget.** With a key the Demo plan's 10,000 credits/month binds,
  not the rate. Calls are counted into `api_usage` (`host`, `YYYY-MM`) and checked
  against `http.monthly_budget`: `run.py` logs used/remaining and the projection,
  warns past 80%, and aborts before spending anything if the run would not fit,
  naming the credits left and the reset date. `--ignore-budget` overrides.
- **Cadence.** A 300-coin run costs ~900 credits and ~35 min, so daily runs do not
  fit (~27,000/month). Weekly, or every third day, does. Later runs are cheaper:
  metadata refreshes every 30 days.

## Hourly OHLCV ingestion

The scoring pipeline above is daily and fundamental. This is a second, independent
collector: hourly 1h bars from Binance for the universe coins that trade there,
`python collect.py` (`make collect`). It exists on its own because **an hour of bars
not collected today cannot be collected later** — Binance serves the last ~5 years of
klines, but only the exchange has them, and our own history has to start sometime. It
feeds nothing yet; it only accumulates.

```bash
python collect.py --map            # rebuild the symbol map and stop
python collect.py --backfill 90    # one-time history pull (default: binance.backfill_days)
python collect.py                  # incremental run
python collect.py --remap          # refresh the map, then collect
```

**Not scheduled for you.** To run it hourly, add one line to your own crontab —
nothing in this repo installs it:

```
5 * * * * cd /path/to/truffle && .venv/bin/python collect.py >> data/collect.log 2>&1
```

Anything from every 15 minutes to once a day works: the collector reads the last
stored bar per symbol and fetches only what is missing, paging 1000 bars at a time, so
a skipped day or a fortnight offline costs extra requests and nothing else. Re-running
inside the same hour asks for nothing at all, because a bar is only fetched once it has
closed.

- **Symbol map** (`binance_symbols`). Tickers collide across projects, so symbol
  equality alone is not a mapping. A Binance base asset is matched to a `coin_id`
  only when the symbol matches a coin in the latest run's universe **and** the
  Binance price agrees with the price CoinGecko last reported within
  `binance.mapping.price_tolerance` (default 2x — loose, because our CoinGecko side
  may be days old, while a genuine collision is normally off by orders of magnitude).
  Futures lot symbols (`1000PEPE`, `1000000MOG`) are divided by the lot size before
  the comparison. Everything else is recorded and **flagged**, never guessed:
  `unmapped` (no universe coin has that ticker — mostly rival exchange tokens and
  coins Binance does not list) or `ambiguous` (the ticker matches but the price does
  not, or several coins match). Where several universe coins share a ticker, the price
  check picks the one that agrees, and only that one. Fix a flagged row by hand with
  `UPDATE binance_symbols SET coin_id=..., status='mapped', tracked=1, manual=1`;
  `manual=1` rows are never overwritten by a later `--map`.
- **Coverage.** ~345 pairs over ~194 of the 300 coins (158 spot, 187 perp). The other
  ~106 are exchange tokens (LEO, OKB, CRO, BGB, GT, KCS, WBT, HTX), tokenised RWAs and
  coins with no Binance USDT pair. Spot and perp are tracked separately: they are
  different venues with different prices, and the perp side carries funding.
- **Storage.** `klines_1h(symbol, market, open_time)` — open/high/low/close, base
  volume, quote volume and trade count per bar — as a `WITHOUT ROWID` table. The
  primary key *is* the table, so there is no duplicate rowid b-tree and one symbol's
  history is a single clustered range scan — which is both the only access pattern that
  matters and a measured 22% off disk (90 vs 117 bytes/row over the 736k-row backfill,
  so ~400 MB per year at 500 symbols). There is deliberately **no
  index on `open_time` alone**: it would roughly double write cost and size, and a
  cross-sectional "every symbol at hour T" query is 345 primary-key seeks, which is
  fast enough. `funding_rates(symbol, funding_time)` stores perp funding the same way.
  Open interest is **not** collected: Binance keeps only 30 days of it, so it can never
  become history.
- **Idempotency.** Bars upsert on their primary key, so a re-run overwrites rather than
  duplicates — which also lets Binance revise a bar. Only *closed* bars are stored; the
  in-progress one is dropped and picked up on the next run.
- **Rate limits.** Binance limits by request **weight** per minute, not call count, and
  reports the running total in `x-mbx-used-weight-1m`. `http.hosts.*.weight_per_min`
  (6000 spot, 2400 futures) drives a weight limiter in the shared HTTP client that
  idles to the next minute at 80% of the cap, steering by the server's own number
  rather than a local estimate. Futures kline weight jumps from 2 to 5 at `limit >= 500`,
  so futures page 499 bars at a time and spot pages 1000. No API key is needed and
  none is sent; `data-api.binance.vision` is configured as a manual fallback host.
- **Bookkeeping.** Each run appends to `ingest_runs` (kind, bars fetched/new, funding
  rows, peak weight, per-symbol failures). Calls are counted into `api_usage` alongside
  CoinGecko's, but Binance has no monthly credit budget, so nothing aborts on it.

## How to add a new metric

1. **Pick the key.** A *visible* metric changes the frozen `docs/api-contract.md` —
   coordinate first. Prefix with `_` to keep it internal.
2. **Write the fetcher** in `truffle/metrics/<source>.py`, taking `http` first and
   calling `http.get_json(...)`. Return `(current, prior)`, `None` for "no data",
   never `0`.
3. **Register the key** in `METRIC_KEYS` in `truffle/pipeline.py`.
4. **Wire it into `Pipeline.collect`** inside `self.safe(...)` so a failure lands in
   `run_errors` and the run continues. No source history? Use
   `prior_from_history(self.con, cid, "<key>", as_of, w)`.
5. **If it can be negative**, add it to `scoring.DIFF_METRICS`.
6. **Add a weight** under `weights` in `config.yaml` if it should score, plus an
   `http.hosts` entry for a new domain. A key in `METRIC_KEYS` with no weight is
   collected and stored but stays out of the composite — the supported way to
   accumulate history for a factor before committing to it. The rest is driven off
   `METRIC_KEYS` and `weights` (the shrinkage factor re-derives itself from the new
   weight set; old runs keep their old scores until recomputed).
7. **Test it** in `tests/test_metric_windows.py` (and `tests/test_history.py` if it
   needs stored history).

No migration needed — `metrics` is long-format, so a new key is just new rows.

## `research/` (not part of the pipeline)

Standalone signal-evaluation code. It imports `truffle.http` / `cache` / `config` so
the rate limits and the monthly CoinGecko budget still apply, reads `data/truffle.db`
read-only, and changes nothing in the pipeline, the scoring module or the API.

```bash
python research/backtest.py --dry-run   # cost projection only
python research/backtest.py             # ~620 CoinGecko credits on a cold cache, then free
python research/paper_log.py            # append today's firings to research/paper_log.jsonl
python research/hourly.py               # hourly rerun against klines_1h; no network, no credits
python research/factor.py               # cross-sectional long-short factors; free on a warm cache
```

`backtest.py` evaluates short-window breakout signals point-in-time against the
production 30d-vs-prior-30d baseline, then simulates trades with fees, slippage,
a stop and a time exit. `--dry-run` prints the projected credit cost and aborts if
it would exceed `http.monthly_budget`. `paper_log.py` costs ~300 credits per run and
is a log only — no orders, no recommendation.

`hourly.py` reruns the same signals on the real hourly Binance bars in `klines_1h`,
with stops checked against actual hourly highs/lows, Binance `quoteVolume` for the
liquidity tiering, an as-of-start universe, and an unconditional "buy anything"
baseline. It reads the database and the response cache only -- no HTTP, no credits.

`factor.py` asks a **structurally different question**. Everything above fires a signal,
buys one coin and holds it — a long-only directional bet dominated by market beta, which
is why the `BASELINE_always` control beat every signal. `factor.py` instead tests whether
the cross-sectional *ranking* carries information once beta is removed: each week it ranks
the as-of top 300 by market cap on eight momentum/reversal variants plus a size factor,
goes equal-weight long the top quintile (and decile) and short the bottom, dollar-neutral,
and charges fees and slippage on the **actual measured turnover**. Short costs use the real
`funding_rates` table for the ~54% of coin-weeks with a mapped Binance perp; coins with no
perp are charged an explicit assumed 15%/yr borrow. It reads the response cache and the
database only — no HTTP, no credits — and reports gross, net at three slippage levels,
breakeven cost, Sharpe, max drawdown, hit rate, a t-statistic with its confidence interval,
and each factor's measured beta to the equal-weight universe.

**Result: momentum is the wrong sign, reversal is the right sign but not significant, and
costs eat both.** Over 40 weekly rebalances (2025-12-18 .. 2026-09-17):

- Cross-sectional momentum was **negative at every lookback before any cost** (quintile
  long-short: −2.64%/wk at 1w, −1.71% at 4w, −1.13% at 12w). Winners lost to losers.
- **Short-term reversal** — the same coefficient with the sign flipped — is the only
  positive cell: +1.84%/wk gross, but t = +0.64 and a 324%/week turnover that puts its
  breakeven at 57 bp/side.
- The most actionable construction (long-only top quintile of 1w reversal minus the
  equal-weight universe, no shorting) is +69.8%/yr at 10 bp and +1.9%/yr at 100 bp,
  t = +1.20. Suggestive, not established.
- **"Dollar-neutral" is not market-neutral**: measured betas to the equal-weight universe
  run +0.30 / +0.28 / +0.11 / −0.28 / +0.22 across the momentum lookbacks.
- Real perp funding is a rounding error next to turnover (a short collected a median
  +0.15%/week). Turnover is what kills these books, not borrow.
- **Nothing in the 36-cell grid passes a Bonferroni correction** (largest |t| = 1.89,
  threshold 3.43), and the equal-weight buy-everything control still beats most of it.

**One year of weekly data cannot establish a factor.** With 40 observations and a ~10.7%
weekly volatility on a quintile dollar-neutral book, the smallest detectable weekly mean is
3.4% — so a real premium of the size the literature reports (single-digit percent per year,
measured over 5+ years in crypto and 5+ decades in equities) is invisible here whether it
exists or not. The module says so in its own output, flags its best cells as probable grid
artefacts, and states the direction of the residual survivorship it cannot fix (the pool is
today's top 600, so coins that died are absent — which biases momentum *down* and reversal
*up*). None of this is trading advice.
