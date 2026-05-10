import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getBacklog, getProjects, addBacklogItem, deleteBacklogItem, runBacklogItem } from '../api/client';
import type { BacklogItem, Project } from '../api/client';

const TYPE_META: Record<string, { label: string; cls: string }> = {
  bug:     { label: 'Bug',     cls: 'bg-red-900/50 text-red-300' },
  feature: { label: 'Feature', cls: 'bg-blue-900/50 text-blue-300' },
  chore:   { label: 'Chore',   cls: 'bg-gray-700/50 text-gray-300' },
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'text-gray-400',
  running: 'text-blue-400',
  done:    'text-green-400',
  failed:  'text-red-400',
}

export default function Backlog() {
  const navigate = useNavigate()
  const [items, setItems] = useState<BacklogItem[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [filter, setFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState('all')
  const [projectFilter, setProjectFilter] = useState('all')
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState<number | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newProject, setNewProject] = useState('')
  const [newType, setNewType] = useState('feature')

  const load = async () => {
    try {
      const [data, projs] = await Promise.all([getBacklog(), getProjects()])
      setItems(data)
      setProjects(projs)
    } catch (e) { console.error(e) }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const handleAdd = async () => {
    if (!newTitle.trim()) return
    try {
      await addBacklogItem(newTitle, newProject, newType)
      setNewTitle(''); setNewProject(''); setNewType('feature'); setShowAdd(false)
      await load()
    } catch (e) { console.error(e) }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this item?')) return
    try { await deleteBacklogItem(id); await load() }
    catch (e) { console.error(e) }
  }

  const handleRun = async (id: number) => {
    setRunning(id)
    try { await runBacklogItem(id); await load() }
    catch (e) { console.error(e) }
    finally { setRunning(null) }
  }

  const handlePlan = (item: BacklogItem) => {
    if (!item.project_id) return
    navigate('/projects/' + item.project_id + '/tasks', {
      state: { tab: 'plan', goal: item.description || item.title }
    })
  }

  const filtered = items.filter(i =>
    (filter === 'all' || i.status === filter) &&
    (typeFilter === 'all' || (i.type ?? 'feature') === typeFilter) &&
    (projectFilter === 'all' ||
     (projectFilter === '__adhoc__' ? !i.project_path : i.project_path === projectFilter))
  )

  const projectName = (item: BacklogItem) => {
    if (!item.project_id) return null
    return projects.find(p => p.id === item.project_id)?.name ?? null
  }

  return (
    <div className='space-y-5'>
      <div className='flex items-center justify-between'>
        <h1 className='text-xl font-semibold text-white'>Backlog</h1>
        <button onClick={() => setShowAdd(true)}
          className='px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm'>
          + Log Item
        </button>
      </div>

      {/* Filters */}
      <div className='flex flex-wrap gap-2'>
        {/* Status filter */}
        <div className='flex gap-1 flex-wrap'>
          {(['all', 'pending', 'running', 'done', 'failed'] as const).map(s => (
            <button key={s} onClick={() => setFilter(s)}
              className={'px-3 py-1 rounded text-sm ' + (filter === s ? 'bg-violet-600 text-white' : 'bg-white/5 text-gray-400 hover:bg-white/10')}>
              {s}
            </button>
          ))}
        </div>
        {/* Type filter */}
        <div className='flex gap-1'>
          {(['all', 'bug', 'feature', 'chore'] as const).map(t => (
            <button key={t} onClick={() => setTypeFilter(t)}
              className={'px-3 py-1 rounded text-sm ' + (typeFilter === t
                ? t === 'bug' ? 'bg-red-700 text-white' : t === 'chore' ? 'bg-gray-600 text-white' : 'bg-blue-700 text-white'
                : 'bg-white/5 text-gray-400 hover:bg-white/10')}>
              {t === 'all' ? 'All types' : TYPE_META[t].label}
            </button>
          ))}
        </div>
        {/* Project filter */}
        <select value={projectFilter} onChange={e => setProjectFilter(e.target.value)}
          className='ml-auto bg-[#161927] border border-[#2d3748] rounded px-3 py-1 text-sm text-[#e2e8f0] focus:outline-none focus:border-violet-500'>
          <option value='all'>All projects</option>
          {projects.map(p => <option key={p.id} value={p.project_path}>{p.name}</option>)}
          {items.some(i => !i.project_path) && <option value='__adhoc__'>Adhoc</option>}
        </select>
      </div>

      {loading && <p className='text-gray-400'>Loading...</p>}
      {!loading && filtered.length === 0 && <p className='text-gray-500 text-sm'>No items.</p>}

      <div className='space-y-2'>
        {filtered.map(item => {
          const typeMeta = TYPE_META[item.type ?? 'feature'] ?? TYPE_META.feature
          return (
            <div key={item.id} className='flex items-center gap-3 p-4 bg-[#161927] rounded-xl border border-white/5'>
              <div className='flex-1 min-w-0'>
                <div className='flex items-center gap-2 flex-wrap'>
                  <span className={'px-1.5 py-0.5 rounded text-[10px] font-medium ' + typeMeta.cls}>
                    {typeMeta.label}
                  </span>
                  <p className='text-sm text-white font-medium'>{item.title}</p>
                </div>
                <p className='text-xs text-gray-500 mt-0.5'>
                  {projectName(item)
                    ? <span className='text-violet-400'>{projectName(item)}</span>
                    : item.project_path
                    ? item.project_path.replace(/\\/g, '/').split('/').filter(Boolean).pop()
                    : <span className='text-gray-600 italic'>adhoc</span>}
                </p>
              </div>
              <span className={STATUS_COLORS[item.status] || 'text-gray-400'}>{item.status}</span>
              {item.project_id && item.status === 'pending' && (
                <button onClick={() => handlePlan(item)}
                  className='text-xs px-3 py-1.5 rounded bg-white/5 text-violet-400 hover:bg-violet-500/20 border border-violet-500/20'>
                  Plan
                </button>
              )}
              <button onClick={() => handleRun(item.id)}
                disabled={running === item.id || item.status === 'running'}
                className='text-xs px-3 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white disabled:opacity-40'>
                {running === item.id ? 'Queuing...' : 'Run'}
              </button>
              <button onClick={() => handleDelete(item.id)}
                className='text-xs px-3 py-1 rounded bg-red-500/20 text-red-300 hover:bg-red-500/30'>
                Delete
              </button>
            </div>
          )
        })}
      </div>

      {/* Add item sheet — div overlay (mobile safe) */}
      {showAdd && (
        <div className='fixed inset-0 z-[200] flex flex-col justify-end sm:items-center sm:justify-center'>
          <div className='absolute inset-0 bg-black/60' onClick={() => setShowAdd(false)} />
          <div className='relative w-full sm:max-w-md bg-[#1a1f2e] rounded-t-2xl sm:rounded-xl shadow-2xl p-6'>
            <div className='flex justify-center mb-3 sm:hidden'>
              <div className='w-10 h-1 rounded-full bg-[#4a5568]' />
            </div>
            <h2 className='text-lg font-semibold mb-4 text-white'>Log Backlog Item</h2>

            {/* Type selector */}
            <div className='flex gap-2 mb-3'>
              {(['bug', 'feature', 'chore'] as const).map(t => (
                <button key={t} onClick={() => setNewType(t)}
                  className={'flex-1 py-2 rounded-lg text-sm font-medium border transition-colors ' +
                    (newType === t
                      ? t === 'bug' ? 'bg-red-700 border-red-600 text-white' : t === 'chore' ? 'bg-gray-600 border-gray-500 text-white' : 'bg-blue-700 border-blue-600 text-white'
                      : 'bg-white/5 border-white/10 text-gray-400 hover:bg-white/10')}>
                  {TYPE_META[t].label}
                </button>
              ))}
            </div>

            <div className='space-y-3'>
              <textarea
                value={newTitle}
                onChange={e => setNewTitle(e.target.value)}
                placeholder={newType === 'bug' ? 'Describe the bug...' : newType === 'chore' ? 'Describe the chore...' : 'Describe the feature...'}
                rows={3}
                autoFocus
                className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500 resize-none'
              />
              <input
                value={newProject}
                onChange={e => setNewProject(e.target.value)}
                placeholder='Project path (optional)'
                className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500'
              />
            </div>
            <div className='flex gap-2 mt-4'>
              <button onClick={() => setShowAdd(false)}
                className='flex-1 py-2.5 rounded-lg bg-white/5 text-gray-300 hover:bg-white/10 text-sm'>
                Cancel
              </button>
              <button onClick={handleAdd} disabled={!newTitle.trim()}
                className={'flex-1 py-2.5 rounded-lg text-white text-sm font-medium disabled:opacity-40 ' +
                  (newType === 'bug' ? 'bg-red-700 hover:bg-red-600' : 'bg-violet-600 hover:bg-violet-500')}>
                Add {TYPE_META[newType]?.label}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
