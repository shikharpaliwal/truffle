import { useNavigate } from 'react-router-dom'
import { DASH, METRICS, delta, pct, score, signClass } from '../lib/format'

const COLS = [
  { key: 'market_cap_rank', label: '#', cls: 'rank' },
  { key: null, label: 'Coin', cls: 'left c-name' },
  { key: 'score', label: 'Score', cls: 'c-score' },
  { key: 'score_change', label: 'Change', cls: 'c-score' },
  ...METRICS.map((m) => ({ key: m.key, label: m.label, cls: `m${m.minor ? ' minor' : ''}`, metric: m })),
]

function Header({ col, sort, order, onSort }) {
  const active = col.key === sort
  const left = col.cls === 'rank' || col.cls === 'left c-name'
  return (
    <th className={`${left ? 'left ' : ''}${col.cls.includes('minor') ? 'minor' : ''}`.trim() || undefined} aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : undefined}>
      {col.key ? (
        <button onClick={() => onSort(col.key)} title={col.metric ? 'Sort by 30-day momentum rank' : undefined}>
          {col.label}
          <span className="sort-caret">{active ? (order === 'asc' ? ' ↑' : ' ↓') : ''}</span>
        </button>
      ) : col.label}
    </th>
  )
}

export default function CoinTable({ coins, sort, order, onSort, loading, date }) {
  const nav = useNavigate()
  if (loading) return <p className="empty">Digging&hellip;</p>
  if (!coins?.length) return <p className="empty">Nothing matches those filters.</p>

  return (
    <table className="coins fade">
      <thead>
        <tr>{COLS.map((c, i) => <Header key={c.key ?? i} col={c} sort={sort} order={order} onSort={onSort} />)}</tr>
      </thead>
      <tbody>
        {coins.map((c) => (
          <tr key={c.coin_id} onClick={() => nav(`/coin/${c.coin_id}${date ? `?date=${date}` : ''}`)}>
            <td className="rank num">{c.market_cap_rank ?? DASH}</td>
            <td className="left c-name">
              <span className="coin-cell"><b>{c.name}</b><span className="sym">{c.symbol}</span></span>
            </td>
            <td className="num c-score"><span className="score">{score(c.score)}</span></td>
            <td className={`num c-score ${signClass(c.score_change)}`}>{delta(c.score_change)}</td>
            {METRICS.map((m) => {
              const mv = c.metrics?.[m.key]
              return (
                <td key={m.key} className={`num m${m.minor ? ' minor' : ''}`} data-label={m.label}>
                  <span>
                    {mv?.current == null ? <span className="nil">{DASH}</span> : m.fmt(mv.current)}
                    {mv?.change_pct != null && (
                      <span className={`sub ${signClass(mv.change_pct)}`}>{pct(mv.change_pct, 0)}</span>
                    )}
                  </span>
                </td>
              )
            })}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
