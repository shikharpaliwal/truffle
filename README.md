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
| `GITHUB_TOKEN` | **yes, in practice** | unauthenticated GitHub allows 60 req/hr, not enough for even 20 coins. Any token (no scopes needed) gives 5000/hr. |
| `COINGECKO_API_KEY` | **strongly recommended** | the public tier throttles at ~5 req/min. A free Demo key gives 100/min, capped at **10,000 call credits per calendar month**. |

It runs without either — degrading, recording the failure and marking the affected
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

All six compare the trailing 30 days against the prior 30 (`window_days`).

| metric | what it measures | why it is hard to fake |
|---|---|---|
| `volume_reputable` | 30d volume scaled by the share of ticker volume on exchanges CoinGecko scores green (`trust_score >= 7`) | wash trading is cheap only on unranked venues; tier-1 venues mean real spread, real fees and audited order books |
| `tvl` | mean DefiLlama TVL per window, matched on `gecko_id`; `null` where the coin has no protocol | faking it means actually locking capital in public contracts, at real opportunity cost; DefiLlama strips most double-counting |
| `contributors` | distinct GitHub committers in the period | needs distinct real identities authoring real commits to a repo with real stars |
| `commits` | commit count across the project's main repos | gameable alone, which is why it is weighted below `contributors` |
| `exchange_count` | distinct green-trust-score exchanges listing the coin | listings are gated by third parties who run legal and technical review and charge for it |
| `rel_btc` | coin 30d return − BTC 30d return (`0.0` = flat vs BTC) | the one deliberately market-based signal; subtracting BTC removes beta. Most gameable of the six, so it carries a modest weight |

Repos come from CoinGecko `links.repos_url.github`, capped and star-filtered per
`github.*` in `config.yaml`; commits are fetched incrementally from a per-repo
`last_commit_at` watermark.

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
   f = sqrt(sum q_i^2) / sqrt(sum p_i^2)      # q = weights over ALL metrics, p = over the present ones
   score = 50 + (score_raw - 50) * f          # f <= 1, exactly 1 at full coverage
   ```

   `f` depends only on *which* metrics are present, never on their values, so
   ordering inside a coverage bucket is untouched; only the spread between buckets is
   equalised. A single-metric coin keeps ~42% of its distance from 50, a 5-of-6 coin
   ~93%, a full-coverage coin all of it. `score` is the shrunk number — it is what
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
  no `prior`, so it has no momentum, no `rank_score` and no weight: it is listed under
  `missing` *and* under `pending` (a metric we can read but cannot score yet, as
  opposed to one we never collected), and the coin page marks its chart "awaiting a
  prior window" rather than showing it as a contributing metric. Never faked, never
  zeroed — `weight_coverage` stays at 0.9 instead of claiming the metric counted, and
  the variance shrinkage above prices in the missing weight rather than guessing at it.
  The share is not scored itself: it scales the volume windows, and the prior window
  falls back to today's share until its own history exists. It is stored as
  `_reputable_share`; `_`-prefixed keys never reach the API.
- **Storage.** SQLite at `data/truffle.db`, plain `sqlite3`, idempotent migrations
  in `truffle/db.py`. Runs append only. Raw responses cache to
  `data/cache/<source>/` for `cache.ttl_seconds` (24h).
- **Rate limits.** Per-host `rate_per_min` in `config.yaml`, jittered backoff,
  `Retry-After`, GitHub's `X-RateLimit-*`. A host whose quota resets later than
  `http.max_pause_seconds` is disabled for the rest of the run rather than stalling
  it; its metrics come back `null`. Shipped rates assume both keys are set.
- **Monthly credit budget.** With a key the Demo plan's 10,000 credits/month binds,
  not the rate. Calls are counted into `api_usage` (`host`, `YYYY-MM`) and checked
  against `http.monthly_budget`: `run.py` logs used/remaining and the projection,
  warns past 80%, and aborts before spending anything if the run would not fit,
  naming the credits left and the reset date. `--ignore-budget` overrides.
- **Cadence.** A 300-coin run costs ~900 credits and ~35 min, so daily runs do not
  fit (~27,000/month). Weekly, or every third day, does. Later runs are cheaper:
  metadata refreshes every 30 days, commits are incremental.

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
6. **Add a weight** under `weights` in `config.yaml`, plus an `http.hosts` entry for
   a new domain. The rest is driven off `METRIC_KEYS` and `weights` (the shrinkage
   factor re-derives itself from the new weight set; old runs keep their old scores
   until recomputed).
7. **Test it** in `tests/test_metric_windows.py` (and `tests/test_history.py` if it
   needs stored history).

No migration needed — `metrics` is long-format, so a new key is just new rows.
