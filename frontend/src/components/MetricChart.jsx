import { useState } from 'react'
import { DASH, pct, prettyDate, score, signClass } from '../lib/format'

const W = 300
// A metric we can read today but cannot score yet: its prior window is still being collected.
const AWAITING = 'Awaiting a prior window — not in the score yet.'

// Hand-rolled so nothing arrives styled: one thin line, a baseline, no axes.
export default function MetricChart({ title, points, fmt, metric, wide, pending }) {
  const [hover, setHover] = useState(null)
  const H = wide ? 120 : 76
  const vals = points.filter((p) => p.v != null)

  if (vals.length < 2) {
    return (
      <div className="chart-cell">
        <div className="chart-head">
          <span className="label">{title}</span>
          <span className={`chart-val ${pending ? 'num' : 'nil'}`}>{pending ? fmt(metric?.current) : DASH}</span>
        </div>
        <div className="chart-empty">{pending ? AWAITING : 'No data for this metric.'}</div>
      </div>
    )
  }

  const lo = Math.min(...vals.map((p) => p.v))
  const hi = Math.max(...vals.map((p) => p.v))
  const span = hi - lo || Math.abs(hi) || 1
  const x = (i) => (i / (points.length - 1)) * W
  const y = (v) => H - 6 - ((v - lo) / span) * (H - 12)

  const d = points.reduce((acc, p, i) => (p.v == null ? acc : `${acc}${acc ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`), '')
  const shown = hover ?? { v: metric?.current ?? points.at(-1).v }

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const i = Math.round(((e.clientX - r.left) / r.width) * (points.length - 1))
    const p = points[Math.max(0, Math.min(points.length - 1, i))]
    if (p?.v != null) setHover(p)
  }

  return (
    <div className={`chart-cell${wide ? ' wide' : ''}`}>
      <div className="chart-head">
        <span className="label">{title}</span>
        <span className="chart-val num">{fmt(shown.v)}</span>
      </div>
      <svg
        className="chart-svg" style={{ height: H }} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
        aria-label={`${title} across ${points.length} runs`}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
      >
        <line className="chart-base" x1="0" y1={H - 0.5} x2={W} y2={H - 0.5} vectorEffect="non-scaling-stroke" />
        <path className="chart-line" d={d} vectorEffect="non-scaling-stroke" />
        {hover && (
          <>
            <line className="chart-cursor" x1={x(points.indexOf(hover))} y1="0" x2={x(points.indexOf(hover))} y2={H} vectorEffect="non-scaling-stroke" />
            <circle className="chart-dot" cx={x(points.indexOf(hover))} cy={y(hover.v)} r="2.5" vectorEffect="non-scaling-stroke" />
          </>
        )}
      </svg>
      <div className="chart-foot num">
        <span>{hover ? prettyDate(hover.date) : pending ? '' : metric ? `Prior ${fmt(metric.prior)}` : ''}</span>
        {pending ? <span className="chart-pending">{AWAITING}</span> : (
          <span>
            <span className={signClass(metric?.change_pct)}>{pct(metric?.change_pct)}</span>
            {metric?.rank_score != null && <span className="rank-score"> rank {score(metric.rank_score)}</span>}
          </span>
        )}
      </div>
    </div>
  )
}
