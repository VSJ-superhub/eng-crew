import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import ApprovalBanner from './ApprovalBanner'
import SubtaskReviewBanner from './SubtaskReviewBanner'
import { getStacks, getAwaitingApproval, getAwaitingSubtaskReview } from '../api/client'

const NAV = [
  { to: '/', label: 'Dashboard', exact: true },
  { to: '/projects', label: 'Projects', exact: false },
  { to: '/review', label: 'Review', exact: false },
  { to: '/intake', label: 'Intake', exact: false },
  { to: '/stacks', label: 'Stack', exact: false },
]

const STACK_COLORS: Record<string, string> = {
  quality: 'text-violet-400',
  fast:    'text-blue-400',
  budget:  'text-green-400',
  max:     'text-amber-400',
}

export default function Layout() {
  const [activeStack, setActiveStack] = useState('')
  const [ollamaAvailable, setOllamaAvailable] = useState(true)
  const [reviewCount, setReviewCount] = useState(0)

  useEffect(() => {
    getStacks().then(d => {
      setActiveStack(d.active)
      setOllamaAvailable(d.ollama_available)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const load = () =>
      Promise.all([getAwaitingApproval(), getAwaitingSubtaskReview()])
        .then(([ids, sr]) => setReviewCount(ids.length + sr.reviews.length))
        .catch(() => {})
    load()
    const iv = setInterval(load, 5000)
    return () => clearInterval(iv)
  }, [])

  return (
    <div className="flex h-screen overflow-hidden bg-[#0f1117] text-[#e2e8f0]">
      {/* Sidebar — desktop only */}
      <aside className="hidden sm:flex w-52 flex-shrink-0 bg-[#161927] border-r border-[#2d3748] flex-col">
        <div className="px-4 py-5 border-b border-[#2d3748]">
          <span className="text-lg font-bold text-[#a78bfa]">AI Team</span>
        </div>
        <nav className="flex-1 px-2 py-4 space-y-1">
          {NAV.map(({ to, label, exact }) => (
            <NavLink
              key={to}
              to={to}
              end={exact}
              className={({ isActive }) =>
                [
                  'flex items-center justify-between px-3 py-2 rounded-md text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-[#a78bfa]/20 text-[#a78bfa]'
                    : 'text-[#94a3b8] hover:bg-[#2d3748] hover:text-[#e2e8f0]',
                ].join(' ')
              }
            >
              {label}
              {label === 'Review' && reviewCount > 0 && (
                <span className="ml-1.5 bg-amber-500 text-black text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                  {reviewCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Active stack indicator */}
        {activeStack && (
          <div className="px-3 py-3 border-t border-[#2d3748]">
            <p className="text-[10px] uppercase tracking-wider text-[#4a5568] font-semibold mb-1">Active Stack</p>
            <p className={`text-xs font-medium capitalize ${STACK_COLORS[activeStack] ?? 'text-[#94a3b8]'}`}>{activeStack}</p>
            {activeStack === 'local' && !ollamaAvailable && (
              <p className='text-[10px] text-red-400 mt-0.5'>⚠ Ollama not running</p>
            )}
          </div>
        )}
      </aside>

      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <ApprovalBanner />
        <SubtaskReviewBanner />
        <main className="flex-1 overflow-y-auto p-4 sm:p-6 pb-20 sm:pb-6">
          <Outlet />
        </main>

        {/* Bottom nav — mobile only */}
        <nav className="sm:hidden fixed bottom-0 left-0 right-0 bg-[#161927] border-t border-[#2d3748] flex z-50">
          {NAV.map(({ to, label, exact }) => (
            <NavLink
              key={to}
              to={to}
              end={exact}
              className={({ isActive }) =>
                [
                  'flex-1 flex flex-col items-center py-3 text-[11px] font-medium transition-colors relative',
                  isActive ? 'text-[#a78bfa]' : 'text-[#94a3b8]',
                ].join(' ')
              }
            >
              {label === 'Review' && reviewCount > 0 && (
                <span className="absolute top-1.5 right-1/4 bg-amber-500 text-black text-[9px] font-bold w-4 h-4 flex items-center justify-center rounded-full leading-none">
                  {reviewCount}
                </span>
              )}
              {label}
            </NavLink>
          ))}
        </nav>
      </div>
    </div>
  )
}
