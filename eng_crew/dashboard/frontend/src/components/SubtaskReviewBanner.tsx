import { useEffect, useState } from 'react'
import { getAwaitingSubtaskReview, resolveSubtaskReview, SubtaskReview } from '../api/client'

const AGCOLORS: Record<string, string> = {
  frontend: 'bg-blue-900/50 text-blue-300',
  backend: 'bg-green-900/50 text-green-300',
  database: 'bg-orange-900/50 text-orange-300',
  ai_pipeline: 'bg-purple-900/50 text-purple-300',
  infrastructure: 'bg-yellow-900/50 text-yellow-300',
  generic: 'bg-gray-700/50 text-gray-300',
}

function SubtaskReviewModal({ review, onDone }: { review: SubtaskReview; onDone: () => void }) {
  const [submitting, setSubmitting] = useState(false)
  const files = review.target_files ? review.target_files.split(',').filter(Boolean) : []

  const handle = async (approved: boolean) => {
    setSubmitting(true)
    try {
      await resolveSubtaskReview(review.run_id, approved)
      onDone()
    } catch {
      // ignore — still unblock the UI
      onDone()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className='fixed inset-0 z-[200] flex flex-col justify-end sm:items-center sm:justify-center'>
      <div className='absolute inset-0 bg-black/60 backdrop-blur-sm' />
      <div className='relative z-10 w-full max-w-lg bg-[#161927] border border-white/10 rounded-t-2xl sm:rounded-2xl p-5 shadow-2xl'>
        <div className='flex items-center justify-between mb-3'>
          <h2 className='text-base font-semibold text-white'>
            {review.tests_passed ? '✅' : '⚠️'} Subtask #{review.subtask_id} complete
          </h2>
          <span className='text-xs text-gray-500'>Run #{review.run_id}</span>
        </div>

        <p className='text-sm text-gray-200 mb-3'>{review.description}</p>

        <div className='flex flex-wrap gap-2 mb-3'>
          <span className={`text-xs px-2 py-0.5 rounded ${AGCOLORS[review.agent_type] ?? AGCOLORS.generic}`}>
            {review.agent_type || 'generic'}
          </span>
          {!review.tests_passed && (
            <span className='text-xs px-2 py-0.5 rounded bg-amber-900/50 text-amber-300'>tests failed</span>
          )}
        </div>

        {files.length > 0 && (
          <div className='mb-3'>
            <p className='text-xs text-gray-500 mb-1'>Files changed</p>
            <div className='flex flex-wrap gap-1'>
              {files.map(f => (
                <span key={f} className='text-xs bg-white/5 text-gray-300 px-1.5 py-0.5 rounded font-mono'>
                  {f.split('/').pop()}
                </span>
              ))}
            </div>
          </div>
        )}

        {review.exec_summary && (
          <pre className='text-xs text-gray-400 bg-black/30 rounded p-2 mb-4 max-h-24 overflow-auto whitespace-pre-wrap'>
            {review.exec_summary}
          </pre>
        )}

        <p className='text-xs text-gray-500 mb-4'>Auto-continues in 30 min if no response.</p>

        <div className='flex gap-2'>
          <button
            onClick={() => handle(true)}
            disabled={submitting}
            className='flex-1 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm font-medium transition-colors disabled:opacity-50'
          >
            ✅ Continue to next subtask
          </button>
          <button
            onClick={() => handle(false)}
            disabled={submitting}
            className='px-4 py-2 rounded-lg bg-red-900/40 hover:bg-red-800/60 text-red-300 text-sm font-medium transition-colors disabled:opacity-50'
          >
            🛑 Stop
          </button>
        </div>
      </div>
    </div>
  )
}

export default function SubtaskReviewBanner() {
  const [reviews, setReviews] = useState<SubtaskReview[]>([])
  const [activeReview, setActiveReview] = useState<SubtaskReview | null>(null)

  const load = () =>
    getAwaitingSubtaskReview()
      .then(d => {
        setReviews(d.reviews)
        if (d.reviews.length > 0 && !activeReview) setActiveReview(d.reviews[0])
      })
      .catch(() => null)

  useEffect(() => {
    load()
    const iv = setInterval(load, 3000)
    return () => clearInterval(iv)
  }, [])

  if (reviews.length === 0) return null

  const current = activeReview ?? reviews[0]

  return (
    <>
      <div className='sticky top-0 z-[100] flex items-center justify-between px-4 py-2 bg-violet-900/40 border-b border-violet-700/50 text-violet-200 text-sm'>
        <div className='flex items-center gap-2'>
          <span className='animate-pulse'>🔵</span>
          <span>Subtask #{current.subtask_id} complete — review before next runs</span>
          {reviews.length > 1 && (
            <span className='ml-2 bg-violet-700/60 text-violet-100 text-xs px-2 py-0.5 rounded-full'>
              {reviews.length} pending
            </span>
          )}
        </div>
        <button
          className='ml-2 px-3 py-1 bg-violet-600 hover:bg-violet-500 text-white rounded text-sm font-medium'
          onClick={() => setActiveReview(current)}
        >
          Review
        </button>
      </div>
      {activeReview && (
        <SubtaskReviewModal
          review={activeReview}
          onDone={() => { setActiveReview(null); load() }}
        />
      )}
    </>
  )
}
