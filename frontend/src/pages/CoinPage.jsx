import { useQuery } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getCoin } from '../api'
import LoadError from '../components/LoadError'
import Masthead from '../components/Masthead'
import MetricChart from '../components/MetricChart'
import { DASH, METRICS, delta, prettyDate, score, signClass } from '../lib/format'

const Figure = ({ label, value, cls }) => (
  <div className="figure">
    <span className="label">{label}</span>
    <span className={`figure-val ${cls || ''}`}>{value}</span>
  </div>
)

export default function CoinPage() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const date = params.get('date') || undefined

  const { data, isPending, isError, error } = useQuery({ queryKey: ['coin', id, date], queryFn: () => getCoin(id, { date }) })

  const body = () => {
    if (isPending) return <p className="state">Digging&hellip;</p>
    if (isError) return <LoadError message={error.message} />

    const { coin, latest, history } = data
    const meta = [
      ['Categories', coin.categories?.length ? <span className="tags">{coin.categories.map((c) => <span key={c}>{c}</span>)}</span> : DASH],
      ['GitHub', coin.github_repos?.length
        ? coin.github_repos.map((r) => <div key={r}><a href={`https://github.com/${r}`} target="_blank" rel="noreferrer">{r}</a></div>)
        : DASH],
      ['DefiLlama', coin.defillama_slug
        ? <a href={`https://defillama.com/protocol/${coin.defillama_slug}`} target="_blank" rel="noreferrer">{coin.defillama_slug}</a>
        : DASH],
      ['Homepage', coin.homepage ? <a href={coin.homepage} target="_blank" rel="noreferrer">{coin.homepage.replace(/^https?:\/\//, '')}</a> : DASH],
    ]

    return (
      <>
        <div className="coin-head fade">
          <div className="coin-title">
            <h1>{coin.name}</h1>
            <span className="sym">{coin.symbol}</span>
            {latest?.market_cap_rank && <span className="sym num"> &middot; Rank {latest.market_cap_rank}</span>}
          </div>
          <div className="coin-figures">
            <Figure label="Score" value={score(latest?.score)} />
            <Figure label="Change" value={delta(latest?.score_change)} cls={signClass(latest?.score_change)} />
            <Figure label="Prior" value={score(latest?.score_prev)} />
          </div>
        </div>
        <hr className="hair" />

        <dl className="meta-grid">
          {meta.map(([k, v]) => (
            <div className="meta" key={k}>
              <dt className="label">{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
        <hr className="hair" />

        <p className="label" style={{ padding: '26px 0 6px' }}>
          Metric history &middot; {history.length} runs to {prettyDate(latest?.run_date || history.at(-1)?.run_date)}
        </p>
        <MetricChart
          wide
          title="Composite score"
          fmt={score}
          points={history.map((h) => ({ date: h.run_date, v: h.score ?? null }))}
          metric={{
            current: latest?.score ?? null,
            prior: latest?.score_prev ?? null,
            change_pct: latest?.score_prev ? ((latest.score - latest.score_prev) / latest.score_prev) * 100 : null,
          }}
        />
        <div className="charts">
          {METRICS.map((m) => (
            <MetricChart
              key={m.key}
              title={m.full}
              fmt={m.fmt}
              points={history.map((h) => ({ date: h.run_date, v: h.metrics?.[m.key]?.current ?? null }))}
              metric={latest?.metrics?.[m.key]}
              pending={latest?.pending?.includes(m.key)}
            />
          ))}
        </div>
      </>
    )
  }

  return (
    <div className="shell">
      <Masthead>
        <Link to={date ? `/?date=${date}` : '/'} className="back">&larr; All coins</Link>
      </Masthead>
      <hr className="hair" />
      {body()}
    </div>
  )
}
