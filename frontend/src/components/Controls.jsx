import { usd } from '../lib/format'

const STEPS = [0, 1e6, 1e7, 1e8, 1e9]

export default function Controls({ q, onQ, minVolume, onMinVolume, total }) {
  return (
    <div className="controls">
      <div className="control">
        <span className="label">Search</span>
        <input
          className="search-input"
          value={q}
          onChange={(e) => onQ(e.target.value)}
          placeholder="Name or symbol"
          aria-label="Search coins"
        />
      </div>
      <div className="control">
        <span className="label">Minimum 24h volume</span>
        <div className="steps">
          {STEPS.map((v) => (
            <button key={v} className="step" aria-pressed={minVolume === v} onClick={() => onMinVolume(v)}>
              {v === 0 ? 'Any' : usd(v)}
            </button>
          ))}
        </div>
      </div>
      {total != null && <span className="result-count">{total} coins</span>}
    </div>
  )
}
