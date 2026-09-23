import { useEffect, useState } from 'react'

const KEY = 'truffle-theme'
const read = () => { try { return localStorage.getItem(KEY) } catch { return null } }
const write = (v) => { try { localStorage.setItem(KEY, v) } catch { /* storage unavailable */ } }

const systemDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches

export function useTheme() {
  const [theme, setTheme] = useState(() => read() || (systemDark() ? 'dark' : 'light'))

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  return [theme, () => setTheme((t) => { const n = t === 'dark' ? 'light' : 'dark'; write(n); return n })]
}
