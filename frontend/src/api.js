class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status }
}

export const UNREACHABLE = "Couldn't reach the API on :8000. Start it with `make api`, then reload."

const qs = (params = {}) => {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') u.set(k, v)
  const s = u.toString()
  return s ? `?${s}` : ''
}

// Transport failures and 5xx (including the dev proxy's 502 when nothing is listening)
// surface as an unreachable-backend error; 4xx keeps the backend's own detail.
// A reachable backend with no runs answers 200 / run_date null -- that is an empty state.
async function get(path, params) {
  let res
  try {
    res = await fetch(`/api${path}${qs(params)}`)
  } catch {
    throw new ApiError(0, UNREACHABLE)
  }
  if (!res.ok) {
    if (res.status >= 500) throw new ApiError(res.status, UNREACHABLE)
    const body = await res.json().catch(() => ({}))
    throw new ApiError(res.status, body.detail || res.statusText)
  }
  return res.json()
}

export const getRuns = () => get('/runs')
export const getCoins = (p) => get('/coins', p)
export const getTruffles = (p) => get('/truffles', p)
export const getCoin = (id, p) => get(`/coins/${encodeURIComponent(id)}`, p)
