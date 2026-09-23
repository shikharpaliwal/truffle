import { prettyDate } from '../lib/format'

export default function RunPicker({ runs, value, onChange }) {
  if (!runs?.length) return null
  return (
    <select className="select-bare" value={value || runs[0].run_date} onChange={(e) => onChange(e.target.value)} aria-label="Run date">
      {runs.map((r) => (
        <option key={r.run_date} value={r.run_date}>{prettyDate(r.run_date)}</option>
      ))}
    </select>
  )
}
