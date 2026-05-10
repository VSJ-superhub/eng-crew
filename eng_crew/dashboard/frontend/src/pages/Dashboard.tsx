import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getStatus, getEntrolyStatus, Run } from '../api/client'
import RunsTable from '../components/RunsTable'

interface CostRow {
  provider: string
  model: string
  total_cost_usd: number
  total_input_tokens: number
  total_output_tokens: number
  call_count: number
}

interface EntrolyStatus {
  enabled: boolean
  available: boolean
  quality: string
  token_budget: number
}

export default function Dashboard() {
  const [active, setActive] = useState<Run[]>([])
  const [recent, setRecent] = useState<Run[]>([])
  const [costs, setCosts] = useState<CostRow[]>([])
  const [entroly, setEntroly] = useState<EntrolyStatus | null>(null)
  const [error, setError] = useState('')
  const [connected, setConnected] = useState(false)

  const processData = (d: any) => {
    setActive(d.active_runs || [])
    setRecent(d.recent_runs || [])
    const raw = d.cost_by_model as unknown
    if (Array.isArray(raw)) {
      setCosts(raw as CostRow[])
    } else if (raw && typeof raw === 'object') {
      setCosts(Object.entries(raw as Record<string, number>).map(([model, total_cost_usd]) => ({
        provider: '', model, total_cost_usd: Number(total_cost_usd),
        total_input_tokens: 0, total_output_tokens: 0, call_count: 0,
      })))
    }
  }

  const load = () => getStatus().then(processData).catch(e => setError(String(e)))

  useEffect(() => {
    let iv: ReturnType<typeof setInterval> | null = null
    let es: EventSource | null = null

    const startSSE = () => {
      if (es) es.close()
      if (iv) { clearInterval(iv); iv = null }
      
      es = new EventSource('/api/status/stream')
      es.onopen = () => {
        setConnected(true)
        setError('')
      }
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data)
          processData(d)
        } catch (err) {
          console.error('SSE parse error', err)
        }
      }
      es.onerror = () => {
        setConnected(false)
        if (es) es.close()
        if (!iv) iv = setInterval(load, 10000)
      }
    }

    load()
    getEntrolyStatus().then(setEntroly).catch(() => {})
    startSSE()

    return () => {
      if (es) es.close()
      if (iv) clearInterval(iv)
    }
  }, [])

  const totalCost = costs.reduce((a, r) => a + Number(r.total_cost_usd), 0)

  const attentionRuns = active.filter(r =>
    r.status === 'awaiting_approval' || r.status === 'awaiting_clarification'
  )

  return (
    <div className='max-w-5xl mx-auto space-y-6'>
      <div className='flex items-center justify-between'>
        <div>
          <div className='flex items-center gap-2'>
            <h1 className='text-2xl font-bold text-[#e2e8f0]'>Dashboard</h1>
            <div className={`w-2 h-2 rounded-full mt-1 ${connected ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]' : 'bg-red-500'}`} title={connected ? 'Connected (Live updates)' : 'Disconnected (Polling fallback)'} />
          </div>
          <p className='text-sm text-[#94a3b8] mt-0.5'>AI Team Pipeline Monitor</p>
        </div>
        <Link to='/intake' className='px-4 py-2 bg-[#a78bfa] hover:bg-[#8b5cf6] text-white rounded-md text-sm font-medium'>+ New Task</Link>
      </div>

      {error && <div className='bg-red-900/30 border border-red-700/50 text-red-300 rounded p-3 text-sm'>{error}</div>}

      {attentionRuns.length > 0 && (
        <div className='sticky top-0 z-[100] flex items-center justify-between px-4 py-2 bg-amber-900/40 border border-amber-700/50 rounded text-amber-200 text-sm'>
          <div className='flex items-center gap-2'>
            <span>&#9888;</span>
            <span>
              {attentionRuns.length === 1
                ? <>Run <strong><Link to={`/run/${attentionRuns[0].id}`} className='underline hover:text-white'>#{attentionRuns[0].id}</Link></strong> {attentionRuns[0].status === 'awaiting_clarification' ? 'awaiting clarification' : 'awaiting approval'}</>
                : <>{attentionRuns.length} runs need attention: {attentionRuns.map((r, i) => (
                    <span key={r.id}>
                      {i > 0 && ', '}
                      <strong><Link to={`/run/${r.id}`} className='underline hover:text-white'>#{r.id}</Link></strong>
                    </span>
                  ))}</>
              }
            </span>
            {attentionRuns.length > 1 && (
              <span className='ml-2 bg-amber-700/60 text-amber-100 text-xs px-2 py-0.5 rounded-full'>
                {attentionRuns.length} pending
              </span>
            )}
          </div>
        </div>
      )}

      {entroly && (
        <div className='bg-[#161927] border border-[#2d3748] rounded-lg p-4'>
          <h2 className='text-sm font-semibold text-[#94a3b8] uppercase tracking-wide mb-3'>Entroly Context Optimizer</h2>
          <div className='flex flex-wrap gap-3 items-center'>
            <div className='bg-[#0f1117] border border-[#2d3748] rounded px-3 py-2'>
              <div className='text-xs text-[#94a3b8]'>Status</div>
              <div className={`text-sm font-medium ${entroly.enabled && entroly.available ? 'text-green-400' : entroly.enabled ? 'text-yellow-400' : 'text-[#64748b]'}`}>
                {entroly.enabled && entroly.available ? 'Active' : entroly.enabled ? 'CLI missing' : 'Disabled'}
              </div>
            </div>
            {entroly.enabled && (
              <>
                <div className='bg-[#0f1117] border border-[#2d3748] rounded px-3 py-2'>
                  <div className='text-xs text-[#94a3b8]'>Quality</div>
                  <div className='text-sm font-mono text-[#a78bfa]'>{entroly.quality}</div>
                </div>
                <div className='bg-[#0f1117] border border-[#2d3748] rounded px-3 py-2'>
                  <div className='text-xs text-[#94a3b8]'>Token Budget</div>
                  <div className='text-sm font-mono text-[#a78bfa]'>{entroly.token_budget.toLocaleString()}</div>
                </div>
                <div className='text-xs text-[#64748b] self-center'>~78% context reduction · Shannon entropy + KKT selection</div>
              </>
            )}
            {!entroly.enabled && (
              <div className='text-xs text-[#64748b] self-center'>Set ENTROLY_ENABLED=1 to enable context compression</div>
            )}
          </div>
        </div>
      )}

      {costs.length > 0 && (
        <div className='bg-[#161927] border border-[#2d3748] rounded-lg p-4'>
          <h2 className='text-sm font-semibold text-[#94a3b8] uppercase tracking-wide mb-3'>Token Cost by Model</h2>
          <div className='flex flex-wrap gap-3'>
            {costs.sort((a, b) => Number(b.total_cost_usd) - Number(a.total_cost_usd)).map((row) => (
              <div key={row.provider + '/' + row.model} className='bg-[#0f1117] border border-[#2d3748] rounded px-3 py-2'>
                <div className='text-xs text-[#94a3b8] truncate max-w-[200px]'>{row.provider}/{row.model}</div>
                <div className='text-sm font-mono text-[#a78bfa]'>${Number(row.total_cost_usd).toFixed(4)}</div>
              </div>
            ))}
            <div className='bg-[#0f1117] border border-[#a78bfa]/30 rounded px-3 py-2'>
              <div className='text-xs text-[#94a3b8]'>Total</div>
              <div className='text-sm font-mono text-[#a78bfa] font-bold'>${totalCost.toFixed(4)}</div>
            </div>
          </div>
        </div>
      )}

      <RunsTable active={active} recent={recent} />
    </div>
  )
}
