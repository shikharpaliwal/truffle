const nf = (opts) => new Intl.NumberFormat('en-US', opts)
const compact = nf({ notation: 'compact', maximumFractionDigits: 1 })
const int = nf({ maximumFractionDigits: 0 })

export const DASH = '—'

export const usd = (v) => (v == null ? DASH : `$${compact.format(v)}`)
export const count = (v) => (v == null ? DASH : int.format(v))
export const score = (v) => (v == null ? DASH : v.toFixed(1))

export const pct = (v, digits = 1) =>
  v == null ? DASH : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}%`

export const delta = (v, digits = 1) =>
  v == null ? DASH : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}`

export const ratioPct = (v) => (v == null ? DASH : pct(v * 100))

export const METRICS = [
  { key: 'volume_reputable', label: 'Volume', full: 'Reputable volume', fmt: usd },
  { key: 'tvl', label: 'TVL', full: 'DefiLlama TVL', fmt: usd, minor: true },
  { key: 'contributors', label: 'Contributors', full: 'GitHub contributors', fmt: count },
  { key: 'commits', label: 'Commits', full: 'Commits', fmt: count, minor: true },
  { key: 'exchange_count', label: 'Exchanges', full: 'Exchange listings', fmt: count, minor: true },
  { key: 'rel_btc', label: 'vs BTC', full: 'Performance vs BTC', fmt: ratioPct },
]

export const signClass = (v) => (v == null ? 'nil' : v > 0 ? 'pos' : v < 0 ? 'neg' : '')

export const prettyDate = (d) =>
  d ? new Date(`${d}T00:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : ''
