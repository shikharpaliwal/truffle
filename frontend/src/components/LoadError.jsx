export default function LoadError({ message }) {
  return (
    <div className="state">
      <p className="label" style={{ marginBottom: 14 }}>Nothing loaded</p>
      <p className="state-lead">{message}</p>
    </div>
  )
}
