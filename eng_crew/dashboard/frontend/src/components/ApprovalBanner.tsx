import { useEffect, useState } from 'react'
import { getAwaitingApproval } from '../api/client'
import ApprovalModal from './ApprovalModal'

export default function ApprovalBanner() {
  const [pendingIds, setPendingIds] = useState<number[]>([])
  const [modalId, setModalId] = useState<number | null>(null)
  const [idx, setIdx] = useState(0)

  useEffect(() => {
    const load = () => getAwaitingApproval().then(ids => { setPendingIds(ids); if (ids.length > 0 && !ids.includes(modalId ?? -1)) setIdx(0) })
    load()
    const iv = setInterval(load, 4000)
    return () => clearInterval(iv)
  }, [])

  if (pendingIds.length === 0) return null
  const currentId = pendingIds[idx] ?? pendingIds[0]

  return (
    <>
      <div className='sticky top-0 z-[100] flex items-center justify-between px-4 py-2 bg-amber-900/40 border-b border-amber-700/50 text-amber-200 text-sm'>
        <div className='flex items-center gap-2'>
          <span>Warning</span>
          <span>Run <strong>#{currentId}</strong> awaiting approval</span>
          {pendingIds.length > 1 && (
            <span className='ml-2 bg-amber-700/60 text-amber-100 text-xs px-2 py-0.5 rounded-full'>
              {pendingIds.length} pending
            </span>
          )}
        </div>
        <div className='flex items-center gap-2'>
          {pendingIds.length > 1 && (
            <>
              <button className='text-amber-300 hover:text-white px-1' onClick={() => setIdx(i => (i - 1 + pendingIds.length) % pendingIds.length)}>prev</button>
              <span className='text-amber-400 text-xs'>{idx + 1}/{pendingIds.length}</span>
              <button className='text-amber-300 hover:text-white px-1' onClick={() => setIdx(i => (i + 1) % pendingIds.length)}>next</button>
            </>
          )}
          <button className='ml-2 px-3 py-1 bg-amber-600 hover:bg-amber-500 text-white rounded text-sm font-medium' onClick={() => setModalId(currentId)}>Review</button>
        </div>
      </div>
      {modalId !== null && (
        <ApprovalModal runId={modalId} onClose={() => setModalId(null)} onApproved={() => { setModalId(null); getAwaitingApproval().then(setPendingIds) }} />
      )}
    </>
  )
}
