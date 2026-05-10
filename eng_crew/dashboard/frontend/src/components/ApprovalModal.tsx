import { useEffect, useState } from 'react'
import { approveRun, getRunDetail, SubtaskPlan } from '../api/client'

const AGCOLORS: Record<string, string> = {
  frontend: 'bg-blue-900/50 text-blue-300',
  backend: 'bg-green-900/50 text-green-300',
  database: 'bg-orange-900/50 text-orange-300',
  ai_pipeline: 'bg-purple-900/50 text-purple-300',
  infrastructure: 'bg-yellow-900/50 text-yellow-300',
  generic: 'bg-gray-700/50 text-gray-300',
}

interface Props { runId: number; onClose: () => void; onApproved: () => void }

export default function ApprovalModal({ runId, onClose, onApproved }: Props) {
  const [plan, setPlan] = useState<SubtaskPlan[]>([])
  const [taskText, setTaskText] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [feedback, setFeedback] = useState('')
  const [showFeedback, setShowFeedback] = useState(false)

  useEffect(() => {
    getRunDetail(runId).then(d => {
      setTaskText(d.run.task_text)
      setPlan(d.plan ?? [])
      setSelected(new Set((d.plan ?? []).map(t => String(t.id))))
    }).catch(e => setError(String(e))).finally(() => setLoading(false))
  }, [runId])

  const toggleTask = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const handleApprove = async () => {
    setSubmitting(true)
    try {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), 15000)
      await approveRun(runId, { approved: true, selected_task_ids: [...selected] })
      clearTimeout(t)
      onApproved()
    } catch (e) {
      setError(e instanceof Error && e.name === 'AbortError' ? 'Request timed out — try again' : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const handleReject = async () => {
    setSubmitting(true)
    try {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), 15000)
      await approveRun(runId, { approved: false, feedback })
      clearTimeout(t)
      onApproved()
    } catch (e) {
      setError(e instanceof Error && e.name === 'AbortError' ? 'Request timed out — try again' : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    // z-[200] beats the bottom nav's z-50
    <div className='fixed inset-0 z-[200] flex flex-col justify-end sm:items-center sm:justify-center'>
      {/* Backdrop */}
      <div className='absolute inset-0 bg-black/70' onClick={!submitting ? onClose : undefined} />

      {/* Sheet — slides up from bottom on mobile, centered modal on desktop */}
      <div className='relative w-full sm:max-w-2xl bg-[#1a1f2e] rounded-t-2xl sm:rounded-xl shadow-2xl flex flex-col'
           style={{ maxHeight: 'calc(100vh - 60px)' }}>

        {/* Drag handle (mobile hint) */}
        <div className='flex justify-center pt-3 pb-1 sm:hidden'>
          <div className='w-10 h-1 rounded-full bg-[#4a5568]' />
        </div>

        {/* Header */}
        <div className='flex items-start justify-between px-4 pt-2 pb-3 sm:px-6 sm:pt-5 sm:pb-4 border-b border-[#2d3748]'>
          <div className='flex-1 min-w-0 pr-3'>
            <h2 className='text-base sm:text-lg font-semibold text-[#e2e8f0]'>Review Sprint Plan</h2>
            <p className='text-xs sm:text-sm text-[#94a3b8] mt-0.5 truncate'>Run #{runId} · {taskText}</p>
          </div>
          <button className='text-[#94a3b8] hover:text-[#e2e8f0] text-2xl leading-none mt-0.5 flex-shrink-0'
                  onClick={onClose} disabled={submitting}>×</button>
        </div>

        {/* Content */}
        <div className='flex-1 overflow-y-auto overscroll-contain'>
          {loading ? (
            <div className='flex items-center justify-center h-32 text-[#94a3b8] text-sm'>Loading plan...</div>
          ) : error ? (
            <div className='p-4 text-red-400 text-sm'>{error}</div>
          ) : (
            <div className='divide-y divide-[#2d3748]'>
              {plan.map((t, i) => (
                <label key={t.id} className='flex items-start gap-3 px-4 py-3 sm:px-6 cursor-pointer hover:bg-[#232840] active:bg-[#232840]'>
                  <input
                    type='checkbox'
                    checked={selected.has(String(t.id))}
                    onChange={() => toggleTask(String(t.id))}
                    className='mt-0.5 accent-[#a78bfa] w-4 h-4 flex-shrink-0'
                  />
                  <div className='flex-1 min-w-0'>
                    <div className='flex items-center gap-2 mb-0.5'>
                      <span className='text-xs text-[#94a3b8]'>#{i + 1}</span>
                      <span className={'px-1.5 py-0.5 rounded text-[10px] font-mono ' + (AGCOLORS[t.agent_type] ?? AGCOLORS.generic)}>
                        {t.agent_type}
                      </span>
                    </div>
                    <p className='text-sm text-[#e2e8f0] leading-snug'>{t.description}</p>
                    {t.target_files?.length > 0 && (
                      <p className='text-[11px] text-[#4a5568] mt-0.5 truncate'>{t.target_files.join(', ')}</p>
                    )}
                  </div>
                </label>
              ))}
            </div>
          )}

          {/* Feedback section (shown when rejecting) */}
          {showFeedback && (
            <div className='px-4 py-3 sm:px-6 border-t border-[#2d3748]'>
              <label className='block text-xs text-[#94a3b8] mb-2'>What should the architect change?</label>
              <textarea
                className='w-full h-28 bg-[#0f1117] border border-[#2d3748] rounded-lg p-3 text-[#e2e8f0] text-sm resize-none focus:outline-none focus:border-[#a78bfa]'
                placeholder='Describe what needs to change...'
                value={feedback}
                onChange={e => setFeedback(e.target.value)}
                autoFocus
              />
            </div>
          )}
        </div>

        {/* Footer actions */}
        <div className='px-4 py-3 sm:px-6 sm:py-4 border-t border-[#2d3748] bg-[#161927] rounded-b-2xl sm:rounded-b-xl'>
          {error && <p className='text-red-400 text-xs mb-2'>{error}</p>}
          {showFeedback ? (
            <div className='flex gap-2'>
              <button className='flex-1 py-2.5 rounded-lg bg-[#2d3748] text-[#94a3b8] text-sm'
                      onClick={() => setShowFeedback(false)} disabled={submitting}>Back</button>
              <button className='flex-1 py-2.5 rounded-lg bg-red-700 hover:bg-red-600 text-white text-sm font-medium disabled:opacity-50'
                      onClick={handleReject} disabled={submitting || !feedback.trim()}>
                {submitting ? 'Sending...' : 'Reject with Feedback'}
              </button>
            </div>
          ) : (
            <div className='flex gap-2'>
              <button className='py-2.5 px-4 rounded-lg bg-[#2d3748] text-[#94a3b8] text-sm'
                      onClick={() => setShowFeedback(true)} disabled={submitting || loading}>
                Reject
              </button>
              <button className='flex-1 py-2.5 rounded-lg bg-[#a78bfa] hover:bg-[#8b5cf6] text-white text-sm font-semibold disabled:opacity-50'
                      onClick={handleApprove} disabled={submitting || loading || selected.size === 0}>
                {submitting ? 'Approving...' : `Approve${selected.size < plan.length && plan.length > 0 ? ` (${selected.size}/${plan.length})` : ''}`}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
