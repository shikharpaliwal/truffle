import { Link } from 'react-router-dom'
import { delta, score, signClass, usd } from '../lib/format'

export default function Truffles({ truffles, prevRunDate, loading, date }) {
  return (
    <section className="truffles">
      <div className="truffles-head">
        <div>
          <p className="label" style={{ marginBottom: 10 }}>Today&rsquo;s truffles</p>
          <h2 className="truffles-title">
            {prevRunDate
              ? <>The sharpest risers in composite score since {prevRunDate}.</>
              : <>The sharpest risers in composite score between runs.</>}
          </h2>
        </div>
        <p className="truffles-note">
          Score is the mean percentile rank of 30-day momentum across six fundamentals, measured against the prior 30 days.
        </p>
      </div>

      {loading ? (
        <p className="empty">Digging&hellip;</p>
      ) : !prevRunDate ? (
        <p className="empty">
          Nothing to compare yet &mdash; this is the earliest run. Scores are percentile ranks within one run&rsquo;s
          universe, so risers appear once a second run lands on a later date.
        </p>
      ) : !truffles?.length ? (
        <p className="empty">No coin rose since {prevRunDate}.</p>
      ) : (
        <div className="truffle-grid fade">
          {truffles.map((c, i) => (
            <Link key={c.coin_id} to={`/coin/${c.coin_id}${date ? `?date=${date}` : ''}`} className="truffle">
              <span className="truffle-rank num">{String(i + 1).padStart(2, '0')}</span>
              <span className={`truffle-delta ${signClass(c.score_change)}`}>{delta(c.score_change)}</span>
              <span className="truffle-name">
                <b>{c.name}</b>
                <span className="sym">{c.symbol}</span>
              </span>
              <span className="truffle-foot num">
                Score {score(c.score)} &middot; Vol {usd(c.total_volume)}
              </span>
            </Link>
          ))}
        </div>
      )}
    </section>
  )
}
