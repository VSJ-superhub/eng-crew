import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ApiError, createBacklogItem, deleteBacklogItem, getBacklog, getProjects,
  moveTicket, runTicket, updateBacklogItem,
} from '../api/client';
import type { BacklogItem, Project, TicketStatus } from '../api/client';

const TYPE_META: Record<string, { label: string; cls: string }> = {
  bug:     { label: 'Bug',     cls: 'bg-red-900/50 text-red-300' },
  feature: { label: 'Feature', cls: 'bg-blue-900/50 text-blue-300' },
  chore:   { label: 'Chore',   cls: 'bg-gray-700/50 text-gray-300' },
}

const STATUS_COLORS: Record<string, string> = {
  backlog:     'text-gray-400',
  ready:       'text-violet-300',
  in_progress: 'text-blue-400',
  in_review:   'text-amber-300',
  in_qa:       'text-cyan-300',
  done:        'text-green-400',
  rejected:    'text-red-400',
  shipped:     'text-emerald-400',
}

const STATUS_LABEL: Record<string, string> = {
  backlog: 'backlog', ready: 'ready', in_progress: 'in progress',
  in_review: 'in review', in_qa: 'in QA', done: 'done',
  rejected: 'rejected', shipped: 'shipped',
}

const FILTERS: (TicketStatus | 'all')[] = [
  'all', 'backlog', 'ready', 'in_progress', 'in_review', 'in_qa', 'done', 'rejected',
]

/** The API reports a refused transition as {"error": "..."}; show the reason. */
function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    try { return JSON.parse(e.body || '{}').error ?? e.body } catch { return e.body }
  }
  return String(e)
}

export default function Backlog() {
  const navigate = useNavigate()
  const [items, setItems] = useState<BacklogItem[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [filter, setFilter] = useState<string>('all')
  const [typeFilter, setTypeFilter] = useState('all')
  const [projectFilter, setProjectFilter] = useState('all')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newCriteria, setNewCriteria] = useState('')
  const [newProject, setNewProject] = useState('')
  const [newType, setNewType] = useState('feature')
  const [editing, setEditing] = useState<number | null>(null)
  const [draftCriteria, setDraftCriteria] = useState('')

  const load = async () => {
    try {
      const [data, projs] = await Promise.all([getBacklog(), getProjects()])
      setItems(data)
      setProjects(projs)
    } catch (e) { setError(errorText(e)) }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const act = async (id: number, fn: () => Promise<unknown>) => {
    setBusy(id); setError('')
    try { await fn() }
    catch (e) { setError(errorText(e)) }
    finally { setBusy(null); await load() }
  }

  const handleAdd = async () => {
    if (!newTitle.trim()) return
    setError('')
    try {
      await createBacklogItem({
        title: newTitle.trim(),
        description: newTitle.trim(),
        project_path: newProject,
        item_type: newType,
        acceptance_criteria: newCriteria.trim(),
      })
      setNewTitle(''); setNewCriteria(''); setNewProject(''); setNewType('feature'); setShowAdd(false)
      await load()
    } catch (e) { setError(errorText(e)) }
  }

  const saveCriteria = async (item: BacklogItem) => {
    await act(item.id, async () => {
      await updateBacklogItem(item.id, { acceptance_criteria: draftCriteria })
      setEditing(null)
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
        <div>
          <h1 className='text-xl font-semibold text-white'>Tickets</h1>
          <p className='text-xs text-gray-500 mt-0.5'>
            A ticket needs acceptance criteria before it can be made ready.
          </p>
        </div>
        <button onClick={() => setShowAdd(true)}
          className='px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm'>
          + New Ticket
        </button>
      </div>

      {error && (
        <div className='p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-sm text-red-300'>
          {error}
        </div>
      )}

      {/* Filters */}
      <div className='flex flex-wrap gap-2'>
        <div className='flex gap-1 flex-wrap'>
          {FILTERS.map(s => (
            <button key={s} onClick={() => setFilter(s)}
              className={'px-3 py-1 rounded text-sm ' + (filter === s ? 'bg-violet-600 text-white' : 'bg-white/5 text-gray-400 hover:bg-white/10')}>
              {s === 'all' ? 'all' : STATUS_LABEL[s]}
            </button>
          ))}
        </div>
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
        <select value={projectFilter} onChange={e => setProjectFilter(e.target.value)}
          className='ml-auto bg-[#161927] border border-[#2d3748] rounded px-3 py-1 text-sm text-[#e2e8f0] focus:outline-none focus:border-violet-500'>
          <option value='all'>All projects</option>
          {projects.map(p => <option key={p.id} value={p.project_path}>{p.name}</option>)}
          {items.some(i => !i.project_path) && <option value='__adhoc__'>Adhoc</option>}
        </select>
      </div>

      {loading && <p className='text-gray-400'>Loading...</p>}
      {!loading && filtered.length === 0 && <p className='text-gray-500 text-sm'>No tickets.</p>}

      <div className='space-y-2'>
        {filtered.map(item => {
          const typeMeta = TYPE_META[item.type ?? 'feature'] ?? TYPE_META.feature
          const criteria = (item.acceptance_criteria ?? '').trim()
          return (
            <div key={item.id} className='p-4 bg-[#161927] rounded-xl border border-white/5'>
              <div className='flex items-center gap-3'>
                <div className='flex-1 min-w-0'>
                  <div className='flex items-center gap-2 flex-wrap'>
                    <span className={'px-1.5 py-0.5 rounded text-[10px] font-medium ' + typeMeta.cls}>
                      {typeMeta.label}
                    </span>
                    <p className='text-sm text-white font-medium'>{item.title}</p>
                    {(item.depends_on?.length ?? 0) > 0 && (
                      <span className={'px-1.5 py-0.5 rounded text-[10px] font-medium ' +
                        (item.blocked ? 'bg-amber-500/20 text-amber-300' : 'bg-white/5 text-gray-400')}>
                        {item.blocked
                          ? 'blocked by ' + (item.unmet_prerequisites ?? []).map(p => '#' + p.id).join(', ')
                          : 'after ' + (item.depends_on ?? []).map(id => '#' + id).join(', ')}
                      </span>
                    )}
                  </div>
                  <p className='text-xs text-gray-500 mt-0.5'>
                    {projectName(item)
                      ? <span className='text-violet-400'>{projectName(item)}</span>
                      : item.project_path
                      ? item.project_path.replace(/\\/g, '/').split('/').filter(Boolean).pop()
                      : <span className='text-gray-600 italic'>adhoc</span>}
                  </p>
                </div>
                <span className={STATUS_COLORS[item.status] || 'text-gray-400'}>
                  {STATUS_LABEL[item.status] ?? item.status}
                </span>
                {item.status === 'backlog' && (
                  <button onClick={() => act(item.id, () => moveTicket(item.id, 'ready'))}
                    disabled={busy === item.id || !criteria}
                    title={criteria ? 'Move to ready' : 'Add acceptance criteria first'}
                    className='text-xs px-3 py-1 rounded bg-white/5 text-violet-300 hover:bg-violet-500/20 border border-violet-500/20 disabled:opacity-40'>
                    Mark ready
                  </button>
                )}
                {item.status === 'ready' && (
                  <button onClick={() => act(item.id, () => runTicket(item.id))}
                    disabled={busy === item.id || item.blocked}
                    title={item.blocked
                      ? 'Blocked by ' + (item.unmet_prerequisites ?? []).map(p => '#' + p.id + ' (' + p.status + ')').join(', ')
                      : 'Build this ticket'}
                    className='text-xs px-3 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white disabled:opacity-40'>
                    {busy === item.id ? 'Queuing...' : 'Build'}
                  </button>
                )}
                {item.project_id && (
                  <button onClick={() => navigate('/projects/' + item.project_id + '/tasks')}
                    className='text-xs px-3 py-1 rounded bg-white/5 text-gray-300 hover:bg-white/10'>
                    Queue
                  </button>
                )}
                <button onClick={() => act(item.id, () => deleteBacklogItem(item.id))}
                  className='text-xs px-3 py-1 rounded bg-red-500/20 text-red-300 hover:bg-red-500/30'>
                  Delete
                </button>
              </div>

              {/* Acceptance criteria — the gate on becoming ready, so it is editable here. */}
              <div className='mt-3 pt-3 border-t border-white/5'>
                <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>Acceptance criteria</p>
                {editing === item.id ? (
                  <div className='space-y-2'>
                    <textarea value={draftCriteria} onChange={e => setDraftCriteria(e.target.value)}
                      rows={3} autoFocus
                      placeholder={'- what must be true when this is done'}
                      className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-xs text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500 resize-none' />
                    <div className='flex gap-2'>
                      <button onClick={() => saveCriteria(item)}
                        className='text-xs px-3 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white'>
                        Save
                      </button>
                      <button onClick={() => setEditing(null)}
                        className='text-xs px-3 py-1 rounded bg-white/5 text-gray-300 hover:bg-white/10'>
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <button onClick={() => { setEditing(item.id); setDraftCriteria(criteria) }}
                    className='text-left w-full'>
                    {criteria
                      ? <p className='text-xs text-gray-400 whitespace-pre-wrap hover:text-gray-200'>{criteria}</p>
                      : <p className='text-xs text-amber-500 hover:text-amber-400'>None — add some to make this ready.</p>}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* Add ticket sheet — div overlay (mobile safe) */}
      {showAdd && (
        <div className='fixed inset-0 z-[200] flex flex-col justify-end sm:items-center sm:justify-center'>
          <div className='absolute inset-0 bg-black/60' onClick={() => setShowAdd(false)} />
          <div className='relative w-full sm:max-w-md bg-[#1a1f2e] rounded-t-2xl sm:rounded-xl shadow-2xl p-6'>
            <div className='flex justify-center mb-3 sm:hidden'>
              <div className='w-10 h-1 rounded-full bg-[#4a5568]' />
            </div>
            <h2 className='text-lg font-semibold mb-4 text-white'>New Ticket</h2>

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
              <textarea
                value={newCriteria}
                onChange={e => setNewCriteria(e.target.value)}
                placeholder={'Acceptance criteria — required before this can be made ready\n- ...'}
                rows={3}
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
