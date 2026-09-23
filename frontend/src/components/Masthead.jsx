import { Link } from 'react-router-dom'
import { useTheme } from '../lib/theme'

const Sun = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round">
    <circle cx="8" cy="8" r="3.1" />
    <path d="M8 1v1.6M8 13.4V15M15 8h-1.6M2.6 8H1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1M12.9 12.9l-1.1-1.1M4.2 4.2L3.1 3.1" />
  </svg>
)
const Moon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round">
    <path d="M13.4 9.8A5.8 5.8 0 0 1 6.2 2.6 5.9 5.9 0 1 0 13.4 9.8Z" />
  </svg>
)

export default function Masthead({ children }) {
  const [theme, toggle] = useTheme()
  return (
    <header className="masthead">
      <div className="masthead-row">
        <Link to="/" className="wordmark">
          Truffle
          <span className="wordmark-sub">
            Rising fundamental momentum across the top 300 by market cap. Six signals that are expensive to fake.
          </span>
        </Link>
        <div className="masthead-meta">
          {children}
          <button className="theme-toggle" onClick={toggle} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>
            {theme === 'dark' ? <Sun /> : <Moon />}
          </button>
        </div>
      </div>
    </header>
  )
}
