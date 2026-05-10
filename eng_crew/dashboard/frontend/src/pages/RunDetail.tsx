import { useEffect, useRef, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { getRunDetail, retrySubtask, retryRun, cancelRun, pauseRun, resumeRun, respondClarification } from '../api/client'
import type { RunDetail, RunEvent, SubtaskPlan, CostByAgentEntry, RunDetailResponse } from '../api/client'
import OutputViewer from '../components/OutputViewer'

const AGENT_COLORS: Record<string, string> = {
  frontend: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  backend: 'bg-green-500/20 text-green-300 border-green-500/30',
  database: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  ai_pipeline: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
  infrastructure: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  generic: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
}

function AgentBadge({ type }: { type: string }) {
  const cls = AGENT_COLORS[type] || AGENT_COLORS.generic
  return <span className={'text-xs px-2 py-0.5 rounded border ' + cls}>{type}</span>
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    running: 'bg-blue-500/20 text-blue-300 animate-pulse',
    completed: 'bg-green-500/20 text-green-300',
    done: 'bg-green-500/20 text-green-300',
    failed: 'bg-red-500/20 text-red-300',
    pending: 'bg-gray-500/20 text-gray-400',
    awaiting_approval: 'bg-amber-500/20 text-amber-300',
    paused: 'bg-amber-500/20 text-amber-300',
    awaiting_clarification: 'bg-amber-500/20 text-amber-300 animate-pulse',
  }
  const cls = map[status] || map.pending
  return <span className={'text-xs px-2 py-0.5 rounded ' + cls}>{status.replace(/_/g, ' ')}</span>
}

function SubtaskCard({ st, idx, onRetry }: { st: SubtaskPlan, idx: number, onRetry: (id: string) => void }) {
  const [showDiff, setShowDiff] = useState(false);
  const hasDiff = !!st.diff && st.diff !== "(no changes)";

  return (
    <div className='rounded-xl bg-[#0f1117] border border-white/5 overflow-hidden'>
      <div className='flex items-start gap-4 p-4'>
        <div className='w-6 h-6 rounded-full bg-white/5 border border-white/10 flex items-center justify-center text-[10px] font-bold text-gray-500 shrink-0'>
          {idx + 1}
        </div>
        <div className='flex-1 min-w-0'>
          <p className='text-sm font-medium text-white'>{st.description}</p>
          <div className='flex items-center gap-3 mt-2'>
            <AgentBadge type={st.agent_type || 'generic'} />
            {st.status && <StatusBadge status={st.status} />}
          </div>
        </div>
        <div className='flex items-center gap-2'>
          {hasDiff && (
            <button 
              onClick={() => setShowDiff(!showDiff)}
              className='text-[10px] px-2 py-1 bg-violet-500/10 text-violet-300 rounded border border-violet-500/20 hover:bg-violet-500/20'
            >
              {showDiff ? 'Hide Diff' : 'View Diff'}
            </button>
          )}
          {st.review_passed === false && (
            <button onClick={() => onRetry(st.id)} className='text-[10px] px-2 py-1 bg-red-500/20 text-red-300 rounded border border-red-500/20'>
              Retry
            </button>
          )}
        </div>
      </div>
      
      {showDiff && st.diff && (
        <div className='border-t border-white/5 bg-black/40 p-4 font-mono text-[11px] overflow-x-auto whitespace-pre'>
          {st.diff.split('\n').map((line, i) => {
            let color = 'text-gray-400';
            if (line.startsWith('+') && !line.startsWith('+++')) color = 'text-green-400 bg-green-950/30';
            else if (line.startsWith('-') && !line.startsWith('---')) color = 'text-red-400 bg-red-950/30';
            else if (line.startsWith('@@')) color = 'text-cyan-400';
            else if (line.startsWith('diff') || line.startsWith('index')) color = 'text-violet-400 font-bold';
            
            return <div key={i} className={color}>{line}</div>;
          })}
        </div>
      )}
    </div>
  );
}

function LogViewer({ runId }: { runId: number }) {
  const [logs, setLogs] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const autoScroll = useRef(true)

  useEffect(() => {
    let controller = new AbortController()
    
    const stream = async () => {
      try {
        const res = await fetch(`/api/${runId}/logs`, { signal: controller.signal })
        if (!res.ok) {
          setError(`Failed to load logs: ${res.statusText}`)
          setLoading(false)
          return
        }
        if (!res.body) {
          setError('Response body is empty')
          setLoading(false)
          return
        }

        setLoading(false)
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          const chunk = decoder.decode(value, { stream: true })
          setLogs(prev => prev + chunk)
        }
      } catch (err: any) {
        if (err.name !== 'AbortError') {
          setError(err.message)
        }
      } finally {
        setLoading(false)
      }
    }

    stream()
    return () => controller.abort()
  }, [runId])

  useEffect(() => {
    if (autoScroll.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [logs])

  if (loading) return <div className="p-4 text-gray-500 animate-pulse text-xs">Connecting to log stream...</div>
  if (error) return <div className="p-4 text-red-400 text-xs font-mono border border-red-900/30 rounded-lg bg-red-900/10">Error: {error}</div>

  return (
    <div className="relative group bg-[#0a0c12] rounded-xl border border-white/5 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 bg-white/5 border-b border-white/5">
        <span className="text-[10px] font-bold text-gray-500 uppercase tracking-widest">Raw eng_team Logs</span>
        <button 
          onClick={() => {
            autoScroll.current = !autoScroll.current
            if (autoScroll.current && scrollRef.current) {
               scrollRef.current.scrollTop = scrollRef.current.scrollHeight
            }
          }}
          className={`text-[9px] px-2 py-0.5 rounded transition-colors ${autoScroll.current ? 'bg-violet-500/20 text-violet-300' : 'bg-gray-800 text-gray-500'}`}
        >
          {autoScroll.current ? 'Auto-scroll ON' : 'Auto-scroll OFF'}
        </button>
      </div>
      <div 
        ref={scrollRef}
        className="p-4 h-[600px] overflow-y-auto font-mono text-[11px] leading-relaxed text-gray-300 whitespace-pre scroll-smooth"
        onWheel={() => {
          // If user scrolls up, disable auto-scroll
          if (scrollRef.current) {
            const { scrollTop, scrollHeight, clientHeight } = scrollRef.current
            const atBottom = scrollHeight - scrollTop - clientHeight < 50
            if (!atBottom) autoScroll.current = false
          }
        }}
      >
        {logs || 'Waiting for log data...'}
      </div>
    </div>
  )
}

export default function RunDetail() {
  const { id } = useParams<{ id: string }>()
  const [detail, setDetail] = useState<RunDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [retrying, setRetrying] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [connected, setConnected] = useState(false)
  const [viewMode, setViewMode] = useState<'business' | 'engineering' | 'logs'>('business')
  const [clarificationAnswer, setClarificationAnswer] = useState('')
  const [submittingClarification, setSubmittingClarification] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const handleClarify = async () => {
    if (!id || !clarificationAnswer.trim()) return
    setSubmittingClarification(true)
    try {
      await respondClarification(parseInt(id), clarificationAnswer)
      setClarificationAnswer('')
      await load()
    } catch (e) {
      console.error(e)
    } finally {
      setSubmittingClarification(false)
    }
  }

  const load = async () => {
    if (!id) return
    try {
      const d = await getRunDetail(parseInt(id))
      setDetail(d)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!id) return
    let es: EventSource | null = null
    let iv: ReturnType<typeof setInterval> | null = null

    const startSSE = () => {
      if (es) es.close()
      if (iv) { clearInterval(iv); iv = null }

      es = new EventSource(`/api/runs/${id}/events/stream`)
      es.onopen = () => setConnected(true)
      es.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data)
          if (data.run) {
            setDetail(prev => prev ? { ...prev, run: { ...prev.run, ...data.run } } : null)
          } else if (data.done) {
            setConnected(false)
            es?.close()
          } else if (data.id || data.event_type) {
            const mapped: RunEvent = {
              id: data.id || Date.now(),
              event_type: data.event_type || 'info',
              agent: data.agent_name || data.specialist_name || data.agent || 'unknown',
              message: data.result_text || data.message || '',
              created_at: data.created_at || new Date().toISOString(),
              ...data
            }
            setDetail(prev => prev ? { ...prev, events: [...prev.events, mapped] } : null)
          }
        } catch (err) {
          console.error('SSE parse error', err)
        }
      }
      es.onerror = () => {
        setConnected(false)
        if (es) es.close()
        if (!iv) iv = setInterval(load, 5000)
      }
    }

    load().then(() => {
      const isTerminal = detail?.run.status === 'completed' || detail?.run.status === 'failed'
      if (!isTerminal) startSSE()
    })

    return () => {
      if (es) es.close()
      if (iv) clearInterval(iv)
    }
  }, [id])

  const handlePause = async () => {
    if (!id) return
    setPausing(true)
    try {
      await pauseRun(parseInt(id))
      await load()
    } catch (e) {
      console.error(e)
    } finally {
      setPausing(false)
    }
  }

  const handleResume = async () => {
    if (!id) return
    setResuming(true)
    try {
      await resumeRun(parseInt(id))
      await load()
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : 'Resume failed — process may have restarted. Use Retry instead.')
    } finally {
      setResuming(false)
    }
  }

  const handleCancel = async () => {
    if (!id || !confirm('Mark this run as failed?')) return
    setCancelling(true)
    try {
      await cancelRun(parseInt(id))
      await load()
    } catch (e) {
      console.error(e)
    } finally {
      setCancelling(false)
    }
  }

  const handleRestart = async () => {
    if (!id || !confirm('Restart this task from the beginning?')) return
    try {
      await retryRun(parseInt(id))
      alert('Task restarted successfully.')
      await load()
    } catch (e) {
      console.error(e)
      alert('Failed to restart task.')
    }
  }

  const handleRetry = async (subtaskId: string) => {
    if (!id) return
    setRetrying(subtaskId)
    try {
      await retrySubtask(id, subtaskId)
      await load()
    } catch (e) {
      console.error(e)
    } finally {
      setRetrying(null)
    }
  }

  const copyBranch = (text: string) => {
    navigator.clipboard.writeText(text)
  }

  if (loading) return <div className='flex items-center justify-center h-64 text-gray-400'>Loading...</div>
  if (!detail) return <div className='flex items-center justify-center h-64 text-gray-400'>Run not found.</div>

  const { run, events = [], plan = [], cost_by_agent = {} } = detail
  const durationSec = run.duration_secs ? Math.round(run.duration_secs) : null
  const isTerminal = run.status === 'completed' || run.status === 'failed'

  const currentAgent = events.length > 0 ? events[events.length - 1].agent : null;
  const currentMessage = events.length > 0 ? events[events.length - 1].message : null;

  return (
    <div className='p-6 space-y-6 max-w-5xl mx-auto'>
      <div className='flex items-center justify-between'>
        <div className='flex items-center gap-3'>
          <Link to='/' className='text-gray-500 hover:text-white text-sm'>Dashboard</Link>
          <span className='text-gray-700'>/</span>
          <span className='text-white font-medium'>Run #{id}</span>
          <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]' : isTerminal ? 'bg-gray-600' : 'bg-red-500'}`} />
        </div>
        
        <div className='flex items-center gap-2'>
          {run.status === 'failed' && (
             <button
               onClick={handleRestart}
               className='px-3 py-1 text-xs font-medium rounded bg-violet-600 hover:bg-violet-500 text-white transition-colors'
             >
               Restart Task
             </button>
          )}

          {(run.status === 'paused' || run.status === 'failed') && (
            <button
              onClick={handleResume}
              disabled={resuming}
              className='px-3 py-1 text-xs font-medium rounded bg-green-700/40 hover:bg-green-700/60 text-green-300 border border-green-700/40 transition-colors disabled:opacity-50'
            >
              {resuming ? 'Resuming...' : 'Resume'}
            </button>
          )}

          {!isTerminal && run.status !== 'aborted' && run.status !== 'paused' && (
            <button
              onClick={handlePause}
              className='px-3 py-1 text-xs font-medium rounded bg-amber-700/40 hover:bg-amber-700/60 text-amber-300 border border-amber-700/40 transition-colors'
            >
              Pause
            </button>
          )}

          {!isTerminal && run.status !== 'aborted' && (
            <button
              onClick={handleCancel}
              disabled={cancelling}
              className='px-3 py-1 text-xs font-medium rounded bg-red-700/40 hover:bg-red-700/60 text-red-300 border border-red-700/40 transition-colors disabled:opacity-50'
            >
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
          <div className='flex bg-[#0f1117] p-1 rounded-lg border border-white/5'>
            <button
              onClick={() => setViewMode('business')}
              className={`px-3 py-1 text-xs font-medium rounded ${viewMode === 'business' ? 'bg-violet-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
            >
              Business Summary
            </button>
            <button
              onClick={() => setViewMode('engineering')}
              className={`px-3 py-1 text-xs font-medium rounded ${viewMode === 'engineering' ? 'bg-violet-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
            >
              Engineering
            </button>
            <button
              onClick={() => setViewMode('logs')}
              className={`px-3 py-1 text-xs font-medium rounded ${viewMode === 'logs' ? 'bg-violet-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
            >
              Raw Logs
            </button>
          </div>
        </div>
      </div>

      <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
        <div className='flex justify-between items-start mb-4'>
          <h1 className='text-2xl font-bold text-white'>{run.task_text}</h1>
          <StatusBadge status={run.status} />
        </div>
        
        <div className='grid grid-cols-2 md:grid-cols-4 gap-6 py-4 border-t border-white/5'>
          <div>
            <p className='text-[10px] text-gray-500 uppercase font-bold tracking-wider'>Started</p>
            <p className='text-sm text-gray-300'>{run.started_at ? new Date(run.started_at).toLocaleTimeString() : '—'}</p>
          </div>
          <div>
            <p className='text-[10px] text-gray-500 uppercase font-bold tracking-wider'>Duration</p>
            <p className='text-sm text-gray-300'>{durationSec ? `${durationSec}s` : '—'}</p>
          </div>
          <div>
            <p className='text-[10px] text-gray-500 uppercase font-bold tracking-wider'>Cost</p>
            <p className='text-sm text-violet-400 font-mono'>${Number(run.cost_usd || 0).toFixed(4)}</p>
          </div>
          <div>
            <p className='text-[10px] text-gray-500 uppercase font-bold tracking-wider'>Branch</p>
            <code className='text-[10px] font-mono text-violet-300'>{run.git_branch || '—'}</code>
          </div>
        </div>

        {run.status === 'running' && currentAgent && (
          <div className='mt-4 p-3 bg-blue-500/5 border border-blue-500/20 rounded-lg flex items-center gap-3'>
            <div className='w-2 h-2 rounded-full bg-blue-400 animate-ping' />
            <div className='flex-1 min-w-0'>
              <p className='text-[10px] uppercase text-blue-400 font-bold'>Current Activity</p>
              <p className='text-xs text-gray-300 truncate'>
                <span className='font-bold text-blue-300'>{currentAgent}:</span> {currentMessage}
              </p>
            </div>
          </div>
        )}
      </div>

      {viewMode === 'business' ? (
        <div className='space-y-6'>
          {run.status === 'awaiting_clarification' && detail.clarification && (
            <div className='bg-amber-500/10 rounded-xl p-6 border border-amber-500/30 animate-in fade-in slide-in-from-top-4 duration-500'>
              <h2 className='text-base font-bold text-amber-300 mb-2 flex items-center gap-2'>
                <span className='w-1.5 h-4 bg-amber-500 rounded-full' />
                Agent Needs Clarification
              </h2>
              <p className='text-sm text-gray-200 mb-4 italic'>"{detail.clarification.question}"</p>
              
              {detail.clarification.options && detail.clarification.options.length > 0 && (
                <div className='mb-4 space-y-2'>
                  <p className='text-[10px] uppercase text-amber-400/70 font-bold tracking-wider'>Agent Recommendations</p>
                  <div className='flex flex-wrap gap-2'>
                    {detail.clarification.options.map((opt, i) => (
                      <button
                        key={i}
                        onClick={() => {
                          setClarificationAnswer(opt);
                          // Auto-submit if user clicks a recommendation?
                          // Let's just fill it for now to avoid accidents.
                        }}
                        className='text-xs px-3 py-1.5 bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/20 text-amber-200 rounded-md transition-colors text-left'
                      >
                        {opt}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <div className='space-y-3'>
                <textarea
                  value={clarificationAnswer}
                  onChange={(e) => setClarificationAnswer(e.target.value)}
                  placeholder="Provide an answer to help the agent continue..."
                  className='w-full bg-black/40 border border-white/10 rounded-lg p-3 text-sm text-white focus:outline-none focus:border-amber-500/50 min-h-[80px]'
                />
                <button
                  onClick={handleClarify}
                  disabled={submittingClarification || !clarificationAnswer.trim()}
                  className='w-full py-2 bg-amber-600 hover:bg-amber-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-bold rounded-lg transition-colors'
                >
                  {submittingClarification ? 'Submitting...' : 'Submit Answer & Resume'}
                </button>
              </div>
            </div>
          )}

          {run.final_summary && (
            <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
              <h2 className='text-base font-bold text-white mb-4 flex items-center gap-2'>
                <span className='w-1.5 h-4 bg-violet-500 rounded-full' />
                Executive Summary
              </h2>
              <div className='text-sm text-gray-300 leading-relaxed whitespace-pre-wrap bg-[#0f1117] p-4 rounded-lg border border-white/5'>
                {run.final_summary}
              </div>
            </div>
          )}

          {plan.length > 0 && (
            <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
              <h2 className='text-base font-bold text-white mb-4'>Technical Milestones</h2>
              <div className='space-y-3'>
                {plan.map((st, idx) => (
                  <SubtaskCard key={st.id} st={st} idx={idx} onRetry={handleRetry} />
                ))}
              </div>
            </div>
          )}
        </div>
      ) : viewMode === 'engineering' ? (
        <div className='space-y-6'>
          <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
            <h2 className='text-base font-bold text-white mb-4'>Agent Event Timeline</h2>
            <OutputViewer events={events} />
          </div>

          {Object.keys(cost_by_agent).length > 0 && (
            <div className='bg-[#161927] rounded-xl p-6 border border-white/5'>
              <h2 className='text-base font-bold text-white mb-4'>Resource Allocation</h2>
              <div className='grid grid-cols-1 sm:grid-cols-3 gap-4'>
                {Object.entries(cost_by_agent).map(([agent, data]) => (
                  <div key={agent} className='bg-[#0f1117] border border-white/5 rounded-xl p-4'>
                    <p className='text-[10px] text-gray-500 uppercase font-bold mb-2'>{agent}</p>
                    <div className='flex justify-between items-end'>
                      <span className='text-lg font-mono text-violet-300'>${data.cost_usd.toFixed(4)}</span>
                      <div className='text-right'>
                        <p className='text-[9px] text-gray-600 uppercase font-bold'>Efficiency</p>
                        <p className='text-xs font-mono text-gray-400'>{data.efficiency}</p>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : (
        <LogViewer runId={parseInt(id || '0')} />
      )}
    </div>
  )
}

