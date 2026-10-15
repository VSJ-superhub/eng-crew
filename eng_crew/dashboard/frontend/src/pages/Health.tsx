import { useEffect, useState } from 'react'
import { getHealthMetrics } from '../api/client'
import type { HealthMetrics, TicketPoint } from '../api/client'

function pct(rate: number) {
  return `${Math.round((rate || 0) * 100)}%`
}

function Stat({ label, value, detail }: { label: string, value: string, detail: string }) {
  return (
    <div className='bg-[#161927] rounded-xl p-5 border border-white/5'>
      <p className='text-[10px] text-gray-500 uppercase font-bold tracking-wider'>{label}</p>
      <p className='text-2xl font-bold text-white mt-1'>{value}</p>
      <p className='text-[11px] text-gray-500 mt-1'>{detail}</p>
    </div>
  )
}

/** Cost and wall-clock time per ticket, oldest first — the trend, not a total. */
function TicketSeries({ points }: { points: TicketPoint[] }) {
  const maxCost = Math.max(0.01, ...points.map(p => p.cost_usd || 0))
  const maxSecs = Math.max(1, ...points.map(p => p.duration_secs || 0))

  return (
    <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
      <div className='flex items-baseline justify-between mb-4'>
        <h2 className='text-base font-bold text-white'>Cost and Duration per Ticket</h2>
        <p className='text-[11px] text-gray-500'>{points.length} tickets, oldest first</p>
      </div>
      <div className='space-y-2'>
        {points.map(p => (
          <div key={p.ticket_id} className='flex items-center gap-3 text-xs'>
            <span className='w-10 text-right font-mono text-[10px] text-gray-600'>#{p.ticket_id}</span>
            <span className='w-48 truncate text-gray-300'>{p.title}</span>
            <div className='flex-1 flex items-center gap-2'>
              <div className='flex-1 h-1.5 bg-white/5 rounded overflow-hidden'>
                <div className='h-full bg-violet-500/60' style={{ width: `${((p.cost_usd || 0) / maxCost) * 100}%` }} />
              </div>
              <span className='w-16 text-right font-mono text-violet-300'>${(p.cost_usd || 0).toFixed(3)}</span>
            </div>
            <div className='hidden sm:flex flex-1 items-center gap-2'>
              <div className='flex-1 h-1.5 bg-white/5 rounded overflow-hidden'>
                <div className='h-full bg-blue-500/60' style={{ width: `${((p.duration_secs || 0) / maxSecs) * 100}%` }} />
              </div>
              <span className='w-16 text-right font-mono text-blue-300'>{Math.round(p.duration_secs || 0)}s</span>
            </div>
            <span className='w-14 text-right text-[10px] text-gray-500'>{p.builds} build{p.builds === 1 ? '' : 's'}</span>
          </div>
        ))}
        {points.length === 0 && <p className='text-xs text-gray-500'>No ticket has been built yet.</p>}
      </div>
    </div>
  )
}

export default function Health() {
  const [metrics, setMetrics] = useState<HealthMetrics | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const load = () => getHealthMetrics().then(setMetrics).catch(e => setError(String(e)))
    load()
    const iv = setInterval(load, 15000)
    return () => clearInterval(iv)
  }, [])

  if (error) return <div className='text-red-400 text-sm'>{error}</div>
  if (!metrics) return <div className='flex items-center justify-center h-64 text-gray-400'>Loading…</div>

  const { run_success, review_approval, builds_per_ticket, ticket_series, tool_calls } = metrics

  return (
    <div className='p-2 sm:p-6 space-y-6 max-w-5xl mx-auto'>
      <div>
        <h1 className='text-2xl font-bold text-white'>Health</h1>
        <p className='text-sm text-gray-500'>How eng-crew itself is doing, across every run and ticket.</p>
      </div>

      <div className='grid grid-cols-2 lg:grid-cols-4 gap-4'>
        <Stat
          label='Run success'
          value={pct(run_success.rate)}
          detail={`${run_success.completed} completed · ${run_success.failed} failed · ${run_success.unverified} unverified`}
        />
        <Stat
          label='Review approval'
          value={pct(review_approval.rate)}
          detail={`${review_approval.approve} approved · ${review_approval.changes_requested} changes requested`}
        />
        <Stat
          label='Builds per ticket'
          value={builds_per_ticket.average.toFixed(2)}
          detail={`${builds_per_ticket.builds} runs over ${builds_per_ticket.tickets} tickets`}
        />
        <Stat
          label='Tool calls'
          value={String(tool_calls.total)}
          detail={`${tool_calls.errors} failed`}
        />
      </div>

      <TicketSeries points={ticket_series} />

      {tool_calls.by_tool.length > 0 && (
        <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
          <h2 className='text-base font-bold text-white mb-4'>Tools Used</h2>
          <div className='space-y-1'>
            {tool_calls.by_tool.map(t => (
              <div key={t.tool_name} className='flex items-center gap-3 py-1 text-xs border-b border-white/5 last:border-0'>
                <span className='w-40 truncate text-gray-200'>{t.tool_name}</span>
                <span className='w-20 text-right font-mono text-gray-400'>{t.calls} calls</span>
                <span className='w-20 text-right font-mono text-red-300'>{t.errors} failed</span>
                <span className='flex-1 text-right font-mono text-gray-500'>
                  {Math.round(t.avg_duration_ms)}ms avg
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
