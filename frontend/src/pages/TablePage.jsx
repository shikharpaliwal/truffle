import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getCoins, getRuns, getTruffles } from '../api'
import CoinTable from '../components/CoinTable'
import Controls from '../components/Controls'
import EmptyRuns from '../components/EmptyRuns'
import LoadError from '../components/LoadError'
import Masthead from '../components/Masthead'
import RunPicker from '../components/RunPicker'
import Truffles from '../components/Truffles'

const useDebounced = (value, ms = 250) => {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

export default function TablePage() {
  const [params, setParams] = useSearchParams()
  const date = params.get('date') || undefined
  const sort = params.get('sort') || 'score'
  const order = params.get('order') || 'desc'
  const minVolume = Number(params.get('min_volume') || 0)

  const [qInput, setQInput] = useState(params.get('q') || '')
  const q = useDebounced(qInput)

  const patch = (next) => setParams((prev) => {
    const p = new URLSearchParams(prev)
    for (const [k, v] of Object.entries(next)) (v === '' || v == null || v === 0) ? p.delete(k) : p.set(k, v)
    return p
  }, { replace: true })

  useEffect(() => { patch({ q }) }, [q]) // eslint-disable-line react-hooks/exhaustive-deps

  const runs = useQuery({ queryKey: ['runs'], queryFn: getRuns })
  const listParams = { date, sort, order, q: q || undefined, min_volume: minVolume || undefined, limit: 300 }
  const coins = useQuery({ queryKey: ['coins', listParams], queryFn: () => getCoins(listParams), placeholderData: (p) => p })
  const truffleParams = { date, limit: 6, min_volume: minVolume || undefined }
  const truffles = useQuery({ queryKey: ['truffles', truffleParams], queryFn: () => getTruffles(truffleParams), placeholderData: (p) => p })

  const onSort = (key) => patch(key === sort ? { order: order === 'desc' ? 'asc' : 'desc' } : { sort: key, order: 'desc' })

  const runDate = coins.data?.run_date || truffles.data?.run_date
  // backend reachable but never scanned: a real empty state
  const noRuns = !coins.isPending && coins.data && coins.data.run_date == null

  return (
    <div className="shell">
      <Masthead>
        <RunPicker runs={runs.data?.runs} value={runDate} onChange={(d) => patch({ date: d })} />
      </Masthead>
      <hr className="hair" />

      {coins.isError ? (
        <LoadError message={coins.error.message} />
      ) : noRuns ? (
        <EmptyRuns />
      ) : (
        <>
          <Truffles truffles={truffles.data?.truffles} prevRunDate={truffles.data?.prev_run_date} loading={truffles.isPending} date={date} />
          <hr className="hair" />

          <section className="table-section">
            <Controls q={qInput} onQ={setQInput} minVolume={minVolume} onMinVolume={(v) => patch({ min_volume: v })} total={coins.data?.total} />
            <CoinTable coins={coins.data?.coins} sort={sort} order={order} onSort={onSort} loading={coins.isPending} date={date} />
          </section>
        </>
      )}
    </div>
  )
}
