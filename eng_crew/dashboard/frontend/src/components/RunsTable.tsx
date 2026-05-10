import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Run } from '../api/client'

interface Props { active: Run[]; recent: Run[] }

const STATUS_COLORS: Record<string, string> = {
  running: 'bg-blue-900/50 text-blue-300',
  completed: 'bg-green-900/50 text-green-300',
  failed: 'bg-red-900/50 text-red-300',
  pending: 'bg-gray-700/50 text-gray-300',
  awaiting_approval: 'bg-amber-900/50 text-amber-300',
}

function statusBadge(status: string) {
  const cls = STATUS_COLORS[status] ?? STATUS_COLORS.pending
  return <span className={'px-2 py-0.5 rounded text-xs font-medium ' + cls}>{status.replace(/_/g, ' ')}</span>
}

function fmtDuration(secs?: number): string {
  if (!secs) return 'n/a'
  if (secs < 60) return Math.round(secs) + 's'
  return Math.floor(secs / 60) + 'm ' + Math.round(secs % 60) + 's'
}

function fmtCost(cost?: number): string {
  if (cost == null || cost === 0) return 'n/a'
  return '$' + Number(cost).toFixed(4)
}

function projectName(path?: string): string {
  if (!path) return 'n/a'
  const segs = path.split('/')
  return segs.filter(Boolean).pop() ?? path
}

export default function RunsTable({ active, recent }: Props) {
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  const filteredRecent = recent.filter(r => {
    if (statusFilter !== 'all' && r.status !== statusFilter) return false
    if (search && !r.task_text?.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  return (
    <div className='space-y-6'>
      <div className='flex items-center gap-3'>
        <input type='text' placeholder='Search...' value={search} onChange={e => setSearch(e.target.value)} className='flex-1 bg-[#161927] border border-[#2d3748] rounded px-3 py-1.5 text-sm text-[#e2e8f0] focus:outline-none focus:border-[#a78bfa]' />
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className='bg-[#161927] border border-[#2d3748] rounded px-3 py-1.5 text-sm text-[#e2e8f0] focus:outline-none'>
          <option value='all'>All</option>
          <option value='running'>Running</option>
          <option value='completed'>Completed</option>
          <option value='failed'>Failed</option>
          <option value='pending'>Pending</option>
          <option value='awaiting_approval'>Awaiting Approval</option>
        </select>
      </div>
      {active.length > 0 && (
        <section>
          <h2 className='text-sm font-semibold text-[#94a3b8] uppercase tracking-wide mb-3'>Active ({active.length})</h2>
          <div className='space-y-3'>
            {active.map(r => {
              const total = r.total_subtasks ?? 0
              const done = r.done_subtasks ?? 0
              const pct = total > 0 ? Math.round((done / total) * 100) : 0
              return (
                <div key={r.id} className='bg-[#161927] border border-[#2d3748] rounded-lg p-4'>
                  <div className='flex items-start justify-between gap-3'>
                    <div className='min-w-0'>
                      <Link to={'/run/' + r.id} className='text-sm text-[#a78bfa] hover:underline font-medium truncate block'>#{r.id} — {r.task_text}</Link>
                      <div className='text-xs text-[#94a3b8] mt-1'>{projectName(r.project_path)}</div>
                    </div>
                    <div className='text-right shrink-0 space-y-1'>
                      {statusBadge(r.status)}
                      <div className='text-xs text-[#94a3b8]'>{fmtCost(r.running_cost ?? r.cost_usd)}</div>
                    </div>
                  </div>
                  <div className='mt-3 flex items-center gap-3'>
                    <div className='flex-1 h-1.5 bg-[#2d3748] rounded-full overflow-hidden'>
                      <div className='h-full bg-blue-500 rounded-full transition-all duration-500'
                        style={{ width: total > 0 ? pct + '%' : '0%' }} />
                    </div>
                    <span className='text-xs text-[#94a3b8] shrink-0'>{done}/{total}</span>
                  </div>
                  {r.current_agent && (
                    <div className='mt-1.5 text-xs text-[#94a3b8]'>
                      <span className='text-[#a78bfa]'>▶</span> {r.current_agent}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      )}
      <section>
        <h2 className='text-sm font-semibold text-[#94a3b8] uppercase tracking-wide mb-3'>Recent Runs</h2>
        {filteredRecent.length === 0 ? (
          <div className='text-[#94a3b8] text-sm py-8 text-center'>No runs found.</div>
        ) : (
          <table className='w-full text-sm'>
            <thead>
              <tr className='text-[#94a3b8] text-left border-b border-[#2d3748]'>
                <th className='pb-2 pr-3 w-12'>#</th>
                <th className='pb-2 pr-3'>Status</th>
                <th className='pb-2 pr-3'>Task</th>
                <th className='pb-2 pr-3'>Project</th>
                <th className='pb-2 pr-3 text-right'>Duration</th>
                <th className='pb-2 text-right'>Cost</th>
              </tr>
            </thead>
            <tbody>
              {filteredRecent.map(r => (
                <tr key={r.id} className='border-b border-[#2d3748]/50 hover:bg-[#161927]'>
                  <td className='py-2.5 pr-3 text-[#94a3b8]'>{r.id}</td>
                  <td className='py-2.5 pr-3'>{statusBadge(r.status)}</td>
                  <td className='py-2.5 pr-3 max-w-xs'>
                    <Link to={'/run/' + r.id} className='text-[#a78bfa] hover:underline truncate block' title={r.task_text}>
                      {r.task_text?.length > 60 ? r.task_text.slice(0, 60) + '...' : r.task_text}
                    </Link>
                  </td>
                  <td className='py-2.5 pr-3 text-[#94a3b8] text-xs'>{projectName(r.project_path)}</td>
                  <td className='py-2.5 pr-3 text-right text-[#94a3b8] text-xs'>{fmtDuration(r.duration_secs)}</td>
                  <td className='py-2.5 text-right text-[#94a3b8] text-xs'>{fmtCost(r.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
