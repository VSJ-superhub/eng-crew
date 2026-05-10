import { useEffect, useRef, useState } from 'react'
import { RunEvent } from '../api/client'

interface Props { events: RunEvent[] }
type Level = 'all' | 'error' | 'warn' | 'info'

function lineColor(type: string): string {
  if (type === 'error') return 'text-red-400'
  if (type === 'warning') return 'text-yellow-400'
  return 'text-[#94a3b8]'
}

function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString('en', { hour12: false }) }
  catch { return iso }
}

function matchesLevel(level: Level, type: string): boolean {
  if (level === 'all') return true
  if (level === 'error') return type === 'error'
  if (level === 'warn') return type === 'warning'
  if (level === 'info') return type !== 'error' && type !== 'warning'
  return true
}

export default function OutputViewer({ events }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const [search, setSearch] = useState('')
  const [level, setLevel] = useState<Level>('all')

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [events.length])

  const visible = events.filter(e => {
    if (!matchesLevel(level, e.event_type)) return false
    if (search && !e.message?.toLowerCase().includes(search.toLowerCase()) && !e.agent?.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const copyAll = () => {
    const text = visible.map(e => '[' + fmtTime(e.created_at) + '] [' + e.agent + '] ' + e.message).join('\n')
    navigator.clipboard.writeText(text)
  }

  const LEVELS: { id: Level; label: string }[] = [{ id: 'all', label: 'ALL' }, { id: 'error', label: 'ERR' }, { id: 'warn', label: 'WARN' }, { id: 'info', label: 'INFO' }]

  return (
    <div className='flex flex-col bg-[#0a0d14] border border-[#2d3748] rounded-lg overflow-hidden'>
      <div className='flex items-center gap-3 px-3 py-2 border-b border-[#2d3748] bg-[#0f1117]'>
        <input type='text' placeholder='Search logs...' value={search} onChange={e => setSearch(e.target.value)}
          className='flex-1 bg-[#161927] border border-[#2d3748] rounded px-2 py-1 text-xs text-[#e2e8f0] focus:outline-none focus:border-[#a78bfa]' />
        <div className='flex gap-1'>
          {LEVELS.map(l => (
            <button key={l.id} onClick={() => setLevel(l.id)}
              className={['px-2 py-1 rounded text-xs font-mono', level === l.id ? 'bg-[#a78bfa]/20 text-[#a78bfa]' : 'text-[#94a3b8] hover:text-[#e2e8f0]'].join(' ')}>
              {l.label}
            </button>
          ))}
        </div>
        <button onClick={copyAll} className='px-2 py-1 rounded text-xs text-[#94a3b8] hover:text-[#e2e8f0] border border-[#2d3748]' title='Copy all'>Copy</button>
      </div>
      <div className='h-96 overflow-y-auto p-3 font-mono text-xs space-y-0.5'>
        {visible.length === 0 ? <div className='text-[#94a3b8]'>No entries.</div> : visible.map(e => {
          const isPreprocessor = e.agent === 'preprocessor'
          return (
            <div key={e.id} className={`flex gap-2 leading-5 ${isPreprocessor ? 'opacity-60 italic' : ''}`}>
              <span className='text-[#4a5568] shrink-0'>{fmtTime(e.created_at)}</span>
              <span className='text-[#a78bfa]/70 shrink-0'>[{isPreprocessor ? '⚙ ' : ''}{e.agent}]</span>
              <span className={lineColor(e.event_type)}>{e.message}</span>
            </div>
          )
        })}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
