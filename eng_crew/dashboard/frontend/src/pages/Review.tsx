import { useEffect, useState } from 'react'
import {
  getAwaitingApproval, getRunDetail, approveRun,
  getAwaitingSubtaskReview, resolveSubtaskReview,
  SubtaskPlan, SubtaskReview,
} from '../api/client'

// ── Shared constants ──────────────────────────────────────────────────────────

const AGCOLORS: Record<string, string> = {
  frontend:       'bg-blue-900/50 text-blue-300',
  backend:        'bg-green-900/50 text-green-300',
  database:       'bg-orange-900/50 text-orange-300',
  ai_pipeline:    'bg-purple-900/50 text-purple-300',
  infrastructure: 'bg-yellow-900/50 text-yellow-300',
  generic:        'bg-gray-700/50 text-gray-300',
}

// ── Plan approval card ────────────────────────────────────────────────────────

function PlanCard({ runId, onDone }: { runId: number; onDone: () => void }) {
  const [plan, setPlan]           = useState<SubtaskPlan[]>([])
  const [taskText, setTaskText]   = useState('')
  const [loading, setLoading]     = useState(true)
  const [selected, setSelected]   = useState<Set<string>>(new Set())
  const [feedback, setFeedback]   = useState('')
  const [showFb, setShowFb]       = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr]             = useState('')

  useEffect(() => {
    getRunDetail(runId)
      .then(d => {
        setTaskText(d.run.task_text)
        setPlan(d.plan ?? [])
        setSelected(new Set((d.plan ?? []).map(t => String(t.id))))
      })
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [runId])

  const toggle = (id: string) =>
    setSelected(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })

  const approve = async () => {
    setSubmitting(true)
    try {
      await approveRun(runId, { approved: true, selected_task_ids: [...selected] })
      onDone()
    } catch (e) { setErr(String(e)); setSubmitting(false) }
  }

  const reject = async () => {
    if (!feedback.trim()) return
    setSubmitting(true)
    try {
      await approveRun(runId, { approved: false, feedback })
      onDone()
    } catch (e) { setErr(String(e)); setSubmitting(false) }
  }

  return (
    <div className="bg-[#161927] border border-amber-700/40 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="flex items-start justify-between px-4 py-3 border-b border-[#2d3748] bg-amber-900/20">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono text-amber-400">RUN #{runId}</span>
            <span className="text-xs bg-amber-700/40 text-amber-300 px-2 py-0.5 rounded-full">awaiting approval</span>
          </div>
          <p className="text-sm text-[#e2e8f0] mt-1 line-clamp-2">{taskText}</p>
        </div>
      </div>

      {/* Sprint plan */}
      <div className="divide-y divide-[#2d3748]">
        {loading ? (
          <div className="px-4 py-6 text-[#94a3b8] text-sm text-center">Loading plan…</div>
        ) : err ? (
          <div className="px-4 py-4 text-red-400 text-sm">{err}</div>
        ) : plan.length === 0 ? (
          <div className="px-4 py-4 text-[#94a3b8] text-sm">No subtasks found.</div>
        ) : (
          plan.map((t, i) => (
            <label key={t.id} className="flex items-start gap-3 px-4 py-3 cursor-pointer hover:bg-[#1e2438]">
              <input
                type="checkbox"
                checked={selected.has(String(t.id))}
                onChange={() => toggle(String(t.id))}
                className="mt-0.5 accent-[#a78bfa] w-4 h-4 flex-shrink-0"
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className="text-xs text-[#94a3b8]">#{i + 1}</span>
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${AGCOLORS[t.agent_type] ?? AGCOLORS.generic}`}>
                    {t.agent_type}
                  </span>
                </div>
                <p className="text-sm text-[#e2e8f0] leading-snug">{t.description}</p>
                {t.target_files?.length > 0 && (
                  <p className="text-[11px] text-[#4a5568] mt-0.5 truncate">{t.target_files.join(', ')}</p>
                )}
              </div>
            </label>
          ))
        )}
      </div>

      {/* Feedback box */}
      {showFb && (
        <div className="px-4 py-3 border-t border-[#2d3748]">
          <label className="block text-xs text-[#94a3b8] mb-2">What should the architect change?</label>
          <textarea
            className="w-full h-24 bg-[#0f1117] border border-[#2d3748] rounded-lg p-3 text-[#e2e8f0] text-sm resize-none focus:outline-none focus:border-[#a78bfa]"
            placeholder="Describe what needs to change…"
            value={feedback}
            onChange={e => setFeedback(e.target.value)}
            autoFocus
          />
        </div>
      )}

      {/* Actions */}
      <div className="px-4 py-3 border-t border-[#2d3748] bg-[#12161f] flex gap-2">
        {err && <p className="text-red-400 text-xs mb-2 w-full">{err}</p>}
        {showFb ? (
          <>
            <button
              className="py-2 px-4 rounded-lg bg-[#2d3748] text-[#94a3b8] text-sm"
              onClick={() => setShowFb(false)} disabled={submitting}
            >Back</button>
            <button
              className="flex-1 py-2 rounded-lg bg-red-700 hover:bg-red-600 text-white text-sm font-medium disabled:opacity-50"
              onClick={reject} disabled={submitting || !feedback.trim()}
            >{submitting ? 'Sending…' : 'Reject with Feedback'}</button>
          </>
        ) : (
          <>
            <button
              className="py-2 px-4 rounded-lg bg-[#2d3748] hover:bg-[#374151] text-[#94a3b8] text-sm"
              onClick={() => setShowFb(true)} disabled={submitting || loading}
            >Reject</button>
            <button
              className="flex-1 py-2 rounded-lg bg-[#a78bfa] hover:bg-[#8b5cf6] text-white text-sm font-semibold disabled:opacity-50"
              onClick={approve} disabled={submitting || loading || selected.size === 0}
            >
              {submitting
                ? 'Approving…'
                : `Approve & Run${selected.size < plan.length && plan.length > 0 ? ` (${selected.size}/${plan.length} subtasks)` : ''}`}
            </button>
          </>
        )}
      </div>
    </div>
  )
}

// ── Subtask review card ───────────────────────────────────────────────────────

function SubtaskCard({ review, onDone }: { review: SubtaskReview; onDone: () => void }) {
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr]               = useState('')
  const files = review.target_files ? review.target_files.split(',').filter(Boolean) : []

  const handle = async (approved: boolean) => {
    setSubmitting(true)
    try {
      await resolveSubtaskReview(review.run_id, approved)
      onDone()
    } catch (e) { setErr(String(e)); setSubmitting(false) }
  }

  return (
    <div className="bg-[#161927] border border-white/10 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="flex items-start justify-between px-4 py-3 border-b border-[#2d3748]">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-mono text-[#94a3b8]">RUN #{review.run_id}</span>
            <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${AGCOLORS[review.agent_type] ?? AGCOLORS.generic}`}>
              {review.agent_type || 'generic'}
            </span>
            {!review.tests_passed && (
              <span className="text-xs px-2 py-0.5 rounded bg-amber-900/50 text-amber-300">tests failed</span>
            )}
            {!!review.tests_passed && (
              <span className="text-xs px-2 py-0.5 rounded bg-green-900/50 text-green-300">tests passed</span>
            )}
          </div>
          <p className="text-sm text-[#e2e8f0] mt-1">{review.description}</p>
        </div>
      </div>

      {/* Files */}
      {files.length > 0 && (
        <div className="px-4 py-2 border-b border-[#2d3748] flex flex-wrap gap-1">
          {files.map(f => (
            <span key={f} className="text-xs bg-white/5 text-gray-300 px-1.5 py-0.5 rounded font-mono">
              {f.split('/').pop()}
            </span>
          ))}
        </div>
      )}

      {/* Exec summary */}
      {review.exec_summary && (
        <pre className="px-4 py-3 text-xs text-gray-400 bg-black/20 max-h-32 overflow-auto whitespace-pre-wrap border-b border-[#2d3748]">
          {review.exec_summary}
        </pre>
      )}

      {/* Actions */}
      <div className="px-4 py-3 bg-[#12161f] flex gap-2">
        {err && <p className="text-red-400 text-xs mb-2 w-full">{err}</p>}
        <button
          className="py-2 px-4 rounded-lg bg-red-900/50 hover:bg-red-800/60 text-red-300 text-sm disabled:opacity-50"
          onClick={() => handle(false)} disabled={submitting}
        >Halt Run</button>
        <button
          className="flex-1 py-2 rounded-lg bg-[#a78bfa] hover:bg-[#8b5cf6] text-white text-sm font-semibold disabled:opacity-50"
          onClick={() => handle(true)} disabled={submitting}
        >{submitting ? 'Continuing…' : 'Continue to Next Subtask'}</button>
      </div>
    </div>
  )
}

// ── Review page ───────────────────────────────────────────────────────────────

export default function Review() {
  const [planIds, setPlanIds]       = useState<number[]>([])
  const [reviews, setReviews]       = useState<SubtaskReview[]>([])
  const [loading, setLoading]       = useState(true)

  const load = () => {
    Promise.all([getAwaitingApproval(), getAwaitingSubtaskReview()])
      .then(([ids, sr]) => { setPlanIds(ids); setReviews(sr.reviews) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    const iv = setInterval(load, 5000)
    return () => clearInterval(iv)
  }, [])

  const total = planIds.length + reviews.length

  return (
    <div className="max-w-2xl mx-auto space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-[#e2e8f0]">Review Queue</h1>
        {!loading && (
          <span className={`text-xs px-2.5 py-1 rounded-full font-medium ${
            total > 0 ? 'bg-amber-700/40 text-amber-300' : 'bg-[#2d3748] text-[#94a3b8]'
          }`}>
            {total === 0 ? 'Nothing pending' : `${total} pending`}
          </span>
        )}
      </div>

      {loading && (
        <div className="text-[#94a3b8] text-sm">Loading…</div>
      )}

      {/* Plan approvals */}
      {planIds.length > 0 && (
        <section className="space-y-4">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-amber-400">
            Sprint Plans — waiting for approval
          </h2>
          {planIds.map(id => (
            <PlanCard key={id} runId={id} onDone={load} />
          ))}
        </section>
      )}

      {/* Subtask reviews */}
      {reviews.length > 0 && (
        <section className="space-y-4">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-[#94a3b8]">
            Subtask Reviews — waiting for sign-off
          </h2>
          {reviews.map(r => (
            <SubtaskCard key={`${r.run_id}-${r.subtask_id}`} review={r} onDone={load} />
          ))}
        </section>
      )}

      {/* Empty state */}
      {!loading && total === 0 && (
        <div className="flex flex-col items-center justify-center py-24 text-center">
          <div className="text-4xl mb-3">✅</div>
          <p className="text-[#94a3b8] text-sm">No pending reviews.</p>
          <p className="text-[#4a5568] text-xs mt-1">New sprint plans and subtask sign-offs will appear here.</p>
        </div>
      )}
    </div>
  )
}
