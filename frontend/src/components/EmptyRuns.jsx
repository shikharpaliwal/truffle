export default function EmptyRuns() {
  return (
    <div className="state">
      <p className="label" style={{ marginBottom: 14 }}>No runs yet</p>
      <p className="state-lead">
        The database is empty. Run <code>make run20</code> to scan the universe and populate it.
      </p>
    </div>
  )
}
