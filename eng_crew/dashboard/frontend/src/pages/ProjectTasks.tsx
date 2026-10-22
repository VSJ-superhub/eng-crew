import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ApiError, createProjectPlan, getProjectPlan, getRoadmap, getTicketQueue, getTicketEvents,
  moveTicket, planFromClaudeMd, runQueue, runTicket, setTicketArea, setTicketBudget,
  ROADMAP_STATES,
} from '../api/client';
import type {
  Project, Roadmap, RoadmapCard, RoadmapState, Ticket, TicketEvent, TicketStatus, Wip,
} from '../api/client';

const STATUS_BADGE: Record<string, string> = {
  backlog:     'bg-gray-500/20 text-gray-400',
  ready:       'bg-violet-500/20 text-violet-300',
  in_progress: 'bg-blue-500/20 text-blue-300',
  in_review:   'bg-amber-500/20 text-amber-300',
  in_qa:       'bg-cyan-500/20 text-cyan-300',
  done:        'bg-green-500/20 text-green-300',
  rejected:    'bg-red-500/20 text-red-300',
  shipped:     'bg-emerald-500/20 text-emerald-300',
}

/** The lifecycle, in order, for the column layout. */
const LANES: TicketStatus[] = ['backlog', 'ready', 'in_progress', 'in_review', 'in_qa', 'done']

const LANE_LABEL: Record<string, string> = {
  backlog: 'Backlog', ready: 'Ready', in_progress: 'In progress',
  in_review: 'In review', in_qa: 'In QA', done: 'Done',
  rejected: 'Rejected', shipped: 'Shipped',
}

/** Mirrors eng_crew/tickets.py. The server refuses anything else, and says why. */
const NEXT: Record<string, TicketStatus[]> = {
  backlog:     ['ready'],
  ready:       ['in_progress', 'backlog'],
  in_progress: ['in_review', 'rejected', 'backlog'],
  in_review:   ['in_qa', 'in_progress', 'rejected'],
  in_qa:       ['done', 'rejected'],
  done:        ['shipped'],
  rejected:    ['ready', 'backlog'],
  shipped:     [],
}

/** The API reports a refusal — an illegal transition, a missing acceptance
 *  criterion, the WIP limit — as {"error": "..."}; show that, not the raw body. */
function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    try { return JSON.parse(e.body || '{}').error ?? e.body } catch { return e.body }
  }
  return String(e)
}

function WipBar({ wip }: { wip: Wip }) {
  const pct = Math.min(100, (wip.in_flight / Math.max(1, wip.limit)) * 100)
  const over = wip.slots <= 0
  return (
    <div className='bg-[#161927] rounded-xl p-4 border border-white/5'>
      <div className='flex items-baseline justify-between mb-2'>
        <p className='text-[10px] uppercase tracking-wider font-semibold text-gray-500'>
          Work in progress
        </p>
        <p className={'text-sm font-mono ' + (over ? 'text-amber-400' : 'text-gray-300')}>
          {wip.in_flight} / {wip.limit}
        </p>
      </div>
      <div className='h-1.5 w-full bg-white/5 rounded-full overflow-hidden'>
        <div className={'h-full transition-all duration-500 ' + (over ? 'bg-amber-500' : 'bg-violet-500')}
             style={{ width: `${pct}%` }} />
      </div>
      <p className='text-[11px] text-gray-600 mt-2'>
        {over
          ? 'At the limit — finish something before starting more.'
          : `${wip.slots} slot${wip.slots === 1 ? '' : 's'} free. There are no sprints: pull from the queue when a slot opens.`}
      </p>
    </div>
  )
}

function TicketHistory({ ticketId }: { ticketId: number }) {
  const [events, setEvents] = useState<TicketEvent[] | null>(null)

  useEffect(() => {
    getTicketEvents(ticketId).then(d => setEvents(d.events)).catch(() => setEvents([]))
  }, [ticketId])

  if (events === null) return <p className='text-[11px] text-gray-600'>Loading history…</p>
  if (events.length === 0) return <p className='text-[11px] text-gray-700 italic'>No history.</p>

  return (
    <ol className='space-y-1'>
      {events.map(e => (
        <li key={e.id} className='text-[11px] text-gray-500 flex gap-2'>
          <span className='font-mono text-gray-600 shrink-0'>{e.created_at}</span>
          <span className='text-gray-300'>
            {e.from_status ? `${e.from_status} → ${e.to_status}` : `created (${e.to_status})`}
          </span>
          <span className='text-violet-400'>{e.actor}</span>
          {e.note && <span className='text-gray-600 truncate'>— {e.note}</span>}
        </li>
      ))}
    </ol>
  )
}

/** `$1.23 of $15.00` — what a ticket has spent against what it may spend. */
function money(n: number): string {
  return '$' + n.toFixed(2)
}

function TicketCard({ ticket, busy, onMove, onRun, onBudget, onArea }: {
  ticket: Ticket
  busy: boolean
  onMove: (id: number, status: TicketStatus) => void
  onRun: (id: number) => void
  onBudget: (id: number, amount: number) => void
  onArea: (id: number, area: string) => void
}) {
  const [open, setOpen] = useState(false)
  const criteria = (ticket.acceptance_criteria ?? '').trim()
  const nexts = NEXT[ticket.status] ?? []
  const limit = ticket.budget_usd
  const spent = ticket.spent_usd ?? 0
  const overBudget = ticket.budget_exhausted === true
  const prereqs = ticket.prerequisites ?? []
  const unmet = ticket.unmet_prerequisites ?? []
  const blocked = ticket.blocked === true

  return (
    <div className='bg-[#161927] rounded-lg border border-white/5 p-3'>
      <div className='flex items-start gap-2'>
        <button onClick={() => setOpen(!open)} className='flex-1 min-w-0 text-left group'>
          <p className='text-sm text-white group-hover:text-violet-300 transition-colors truncate'>
            {ticket.title}
          </p>
          <div className='flex items-center gap-2 mt-1'>
            <span className={'text-[10px] px-1.5 py-0.5 rounded font-bold uppercase ' + (STATUS_BADGE[ticket.status] ?? '')}>
              {LANE_LABEL[ticket.status] ?? ticket.status}
            </span>
            <span className='text-[10px] text-gray-600 font-mono'>p{ticket.priority}</span>
            <span className='text-[10px] text-gray-500'>{ticket.area || 'Unassigned'}</span>
            {!criteria && (
              <span className='text-[10px] text-amber-500' title='A ticket cannot be made ready without acceptance criteria.'>
                no acceptance criteria
              </span>
            )}
            {prereqs.length > 0 && (
              <span className={'text-[10px] ' + (blocked ? 'text-amber-400' : 'text-gray-600')}
                title={blocked
                  ? 'This ticket waits on work that is not finished. A build is refused until it is.'
                  : 'Everything this ticket waited on is finished.'}>
                {blocked
                  ? 'blocked by ' + unmet.map(p => '#' + p.id).join(', ')
                  : 'after ' + prereqs.map(p => '#' + p.id).join(', ')}
              </span>
            )}
            {limit !== undefined && (
              <span className={'text-[10px] font-mono ' + (overBudget ? 'text-red-400' : 'text-gray-600')}
                title={overBudget
                  ? 'This ticket has spent its budget. No further build, repair or review starts until someone raises it.'
                  : 'Spent so far, against this ticket’s budget.'}>
                {money(spent)}/{money(limit)}
              </span>
            )}
          </div>
        </button>
        {ticket.status === 'ready' && (
          <button onClick={() => onRun(ticket.id)} disabled={busy || blocked}
            title={blocked
              ? 'Blocked by ' + unmet.map(p => '#' + p.id + ' (' + p.status + ')').join(', ')
              : 'Build this ticket'}
            className='text-[11px] px-2.5 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white disabled:opacity-40'>
            {busy ? '…' : 'Build'}
          </button>
        )}
      </div>

      {open && (
        <div className='mt-3 pt-3 border-t border-white/5 space-y-3'>
          {ticket.description && (
            <p className='text-xs text-gray-400 whitespace-pre-wrap'>{ticket.description}</p>
          )}
          <div>
            <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>Acceptance criteria</p>
            {criteria
              ? <p className='text-xs text-gray-400 whitespace-pre-wrap'>{criteria}</p>
              : <p className='text-xs text-amber-500'>None yet — add some on the Backlog page before this can be made ready.</p>}
          </div>

          {prereqs.length > 0 && (
            <div>
              <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>Depends on</p>
              {prereqs.map(p => (
                <p key={p.id} className={'text-xs ' +
                  (unmet.some(u => u.id === p.id) ? 'text-amber-400' : 'text-gray-400')}>
                  #{p.id} {p.title} — {p.status}
                </p>
              ))}
            </div>
          )}

          <div>
            <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>Area</p>
            <p className='text-xs text-gray-400'>{ticket.area || 'Unassigned'}</p>
            <button disabled={busy}
              onClick={() => {
                const answer = window.prompt('Area for this ticket (empty to clear):', ticket.area ?? '')
                if (answer !== null) onArea(ticket.id, answer.trim())
              }}
              className='mt-1.5 text-[11px] px-2 py-1 rounded bg-white/5 text-gray-300 hover:bg-white/10 disabled:opacity-40'>
              Change area
            </button>
          </div>

          {limit !== undefined && (
            <div>
              <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>Budget</p>
              <p className={'text-xs ' + (overBudget ? 'text-red-400' : 'text-gray-400')}>
                {money(spent)} spent of {money(limit)}
                {overBudget && ' — work is stopped until someone raises it'}
              </p>
              <button disabled={busy}
                onClick={() => {
                  const answer = window.prompt('New budget for this ticket, in USD:', limit.toFixed(2))
                  const amount = Number(answer)
                  if (answer !== null && Number.isFinite(amount) && amount >= 0) onBudget(ticket.id, amount)
                }}
                className='mt-1.5 text-[11px] px-2 py-1 rounded bg-white/5 text-gray-300 hover:bg-white/10 disabled:opacity-40'>
                Change budget
              </button>
            </div>
          )}

          {nexts.length > 0 && (
            <div className='flex flex-wrap gap-1.5'>
              {nexts.map(next => (
                <button key={next} disabled={busy}
                  onClick={() => onMove(ticket.id, next)}
                  className='text-[11px] px-2 py-1 rounded bg-white/5 text-gray-300 hover:bg-white/10 disabled:opacity-40'>
                  → {LANE_LABEL[next] ?? next}
                </button>
              ))}
            </div>
          )}

          {ticket.run_id && (
            <p className='text-[11px]'>
              <Link to={`/run/${ticket.run_id}`} className='text-gray-500 hover:text-gray-300 underline'>
                View run #{ticket.run_id} →
              </Link>
            </p>
          )}

          <div>
            <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>History</p>
            <TicketHistory ticketId={ticket.id} />
          </div>
        </div>
      )}
    </div>
  )
}

/** The roadmap's four states. Named in eng_crew/tickets.py; the server decides
 *  which one a ticket is in, so this is only how each one looks. */
const ROADMAP_LABEL: Record<RoadmapState, string> = {
  done: 'done', ready_to_start: 'ready to start', in_flight: 'in flight', blocked: 'blocked',
}

const ROADMAP_BADGE: Record<RoadmapState, string> = {
  done:           'bg-green-500/20 text-green-300',
  ready_to_start: 'bg-violet-500/20 text-violet-300',
  in_flight:      'bg-blue-500/20 text-blue-300',
  blocked:        'bg-amber-500/20 text-amber-300',
}

function RoadmapCardView({ card, busy, onArea }: {
  card: RoadmapCard
  busy: boolean
  onArea: (id: number, area: string) => void
}) {
  return (
    <div className='bg-[#161927] rounded-lg border border-white/5 p-3 space-y-1.5'>
      <div className='flex items-start gap-2'>
        <span className='text-[10px] font-mono text-gray-600 shrink-0 pt-0.5'>#{card.id}</span>
        <p className='text-sm text-white flex-1 min-w-0'>{card.title}</p>
      </div>
      <div className='flex flex-wrap items-center gap-2'>
        <span className={'text-[10px] px-1.5 py-0.5 rounded font-bold uppercase ' + ROADMAP_BADGE[card.state]}>
          {ROADMAP_LABEL[card.state]}
        </span>
        <span className='text-[10px] text-gray-500'>{LANE_LABEL[card.status] ?? card.status}</span>
        <button disabled={busy}
          onClick={() => {
            const answer = window.prompt('Area for this ticket (empty to clear):', card.area)
            if (answer !== null) onArea(card.id, answer.trim())
          }}
          title='Move this ticket to another area.'
          className='text-[10px] text-gray-600 hover:text-violet-300 disabled:opacity-40'>
          change area
        </button>
      </div>
      {card.state === 'blocked' && (
        <p className='text-[11px] text-amber-400'>
          waits on {card.unmet_prerequisites.map(p => '#' + p.id + ' (' + p.status + ')').join(', ')}
        </p>
      )}
    </div>
  )
}

/** One column per area, prerequisites above whatever waits on them. The order
 *  and the states both come from the server — see eng_crew/tickets.py. */
function RoadmapView({ projectId }: { projectId: number }) {
  const [board, setBoard] = useState<Roadmap | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(async () => {
    try { setBoard(await getRoadmap(projectId)) }
    catch (e) { setError(errorText(e)) }
  }, [projectId])

  useEffect(() => { load() }, [load])

  const handleArea = async (id: number, area: string) => {
    setBusy(id); setError('')
    try { await setTicketArea(id, area) }
    catch (e) { setError(errorText(e)) }
    finally { setBusy(null); await load() }
  }

  if (!board) {
    return <p className='text-sm text-gray-500 animate-pulse'>Loading roadmap…</p>
  }
  if (board.cards.length === 0) {
    return <p className='text-sm text-gray-500'>No tickets yet — plan the project to fill the roadmap.</p>
  }

  return (
    <div className='space-y-4'>
      <div className='flex flex-wrap gap-2'>
        {ROADMAP_STATES.filter(s => board.counts[s] > 0).map(s => (
          <span key={s} className={'text-[11px] px-2 py-0.5 rounded ' + ROADMAP_BADGE[s]}>
            {board.counts[s]} {ROADMAP_LABEL[s]}
          </span>
        ))}
      </div>

      {error && (
        <div className='p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-sm text-red-300'>{error}</div>
      )}

      <div className='grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4'>
        {board.groups.map(([area, cards]) => (
          <div key={area} className='space-y-2'>
            <div className='flex items-center justify-between px-1'>
              <p className='text-[10px] uppercase tracking-wider font-semibold text-gray-500'>{area}</p>
              <span className='text-[10px] text-gray-600 font-mono'>{cards.length}</span>
            </div>
            {cards.map(c => (
              <RoadmapCardView key={c.id} card={c} busy={busy === c.id} onArea={handleArea} />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function ProjectTasks() {
  const { id } = useParams<{ id: string }>()
  const projectId = id ? parseInt(id) : NaN

  const [project, setProject] = useState<Project | null>(null)
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [wip, setWip] = useState<Wip | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [planning, setPlanning] = useState(false)
  const [planningFailed, setPlanningFailed] = useState(false)
  const [goal, setGoal] = useState('')
  // Two views of the same tickets: the queue (by status) and the roadmap (by
  // area, in dependency order).
  const [tab, setTab] = useState<'queue' | 'roadmap'>('queue')

  const load = useCallback(async () => {
    if (isNaN(projectId)) return
    try {
      const queue = await getTicketQueue(projectId)
      setProject(queue.project)
      setTickets(queue.tickets)
      setWip(queue.wip)
      if (queue.tickets.length === 0) {
        const plan = await getProjectPlan(projectId)
        setPlanning(plan.planning)
        setPlanningFailed(!!plan.planning_failed)
        if (plan.error) setError(plan.error)
      } else {
        setPlanning(false)
        setPlanningFailed(false)
      }
    } catch (e) {
      setError(errorText(e))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => { load() }, [load])

  // Planning and builds both finish out of band; poll while either is live.
  const live = planning || tickets.some(t => t.status === 'in_progress')
  useEffect(() => {
    if (!live) return
    const iv = setInterval(load, 4000)
    return () => clearInterval(iv)
  }, [live, load])

  const act = async (ticketId: number, fn: () => Promise<unknown>) => {
    setBusy(ticketId); setError('')
    try { await fn() }
    catch (e) { setError(errorText(e)) }
    finally { setBusy(null); await load() }
  }

  const handlePlan = async (fromClaudeMd: boolean) => {
    setError(''); setPlanning(true); setPlanningFailed(false)
    try {
      if (fromClaudeMd) await planFromClaudeMd(projectId)
      else await createProjectPlan(projectId, goal || project?.description || project?.name || 'Plan this project', true)
      setGoal('')
    } catch (e) {
      setPlanning(false)
      setError(errorText(e))
    }
    await load()
  }

  const handleRunQueue = async () => {
    setError('')
    try {
      const result = await runQueue(projectId)
      if (result.refused.length > 0) setError(result.refused[0].detail)
      else if (result.started.length === 0) setError('Nothing in the queue is ready to build.')
    } catch (e) {
      setError(errorText(e))
    }
    await load()
  }

  if (loading) return <div className='flex items-center justify-center h-64 text-gray-500 animate-pulse'>Loading tickets…</div>
  if (!project) return <div className='flex items-center justify-center h-64 text-red-400'>Project not found.</div>

  const lanes = LANES.map(status => ({ status, items: tickets.filter(t => t.status === status) }))
  const extras = (['rejected', 'shipped'] as TicketStatus[])
    .map(status => ({ status, items: tickets.filter(t => t.status === status) }))
    .filter(l => l.items.length > 0)

  return (
    <div className='p-6 max-w-7xl mx-auto space-y-6'>
      <nav className='flex items-center gap-2 text-sm'>
        <Link to='/projects' className='text-gray-500 hover:text-white transition-colors'>Projects</Link>
        <span className='text-gray-700'>/</span>
        <span className='text-white font-medium'>{project.name}</span>
      </nav>

      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <h1 className='text-2xl font-bold text-white'>
            {tab === 'queue' ? 'Ticket queue' : 'Roadmap'}
          </h1>
          <p className='text-gray-400 text-sm mt-1'>
            {tab === 'queue'
              ? (project.description || `Continuous queue for ${project.name}.`)
              : 'Every ticket by area, prerequisites first.'}
          </p>
        </div>
        {tab === 'queue' && <button onClick={handleRunQueue}
          disabled={!wip || wip.slots <= 0
                    || tickets.every(t => t.status !== 'ready')
                    || tickets.some(t => t.status === 'in_progress')}
          title='Builds the highest-priority ready ticket.'
          className='px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm disabled:opacity-40'>
          Run queued work
        </button>}
      </div>

      <div className='flex gap-1 border-b border-white/5'>
        {(['queue', 'roadmap'] as const).map(name => (
          <button key={name} onClick={() => setTab(name)}
            className={'px-3 py-2 text-sm -mb-px border-b-2 transition-colors ' + (
              tab === name
                ? 'border-violet-500 text-white'
                : 'border-transparent text-gray-500 hover:text-gray-300')}>
            {name === 'queue' ? 'Queue' : 'Roadmap'}
          </button>
        ))}
      </div>

      {wip && <WipBar wip={wip} />}

      {error && (
        <div className='p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-sm text-red-300'>
          {error}
        </div>
      )}

      {tab === 'roadmap' && <RoadmapView projectId={projectId} />}

      {tab === 'queue' && tickets.length === 0 && (
        <div className='p-8 bg-[#161927] rounded-2xl border border-dashed border-white/10 space-y-4'>
          {planning ? (
            <div className='text-center'>
              <div className='w-6 h-6 border-2 border-blue-400 border-t-transparent rounded-full animate-spin mx-auto mb-4' />
              <p className='text-white font-medium'>Planning…</p>
              <p className='text-xs text-gray-500 mt-1'>Breaking the goal into tickets with acceptance criteria.</p>
            </div>
          ) : (
            <>
              <p className='text-gray-400 text-sm'>
                {planningFailed
                  ? 'The planner finished without producing any tickets.'
                  : 'No tickets yet. Plan the project to fill the queue.'}
              </p>
              <textarea value={goal} onChange={e => setGoal(e.target.value)} rows={3}
                placeholder='What should this project achieve next?'
                className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500 resize-none' />
              <div className='flex gap-2'>
                <button onClick={() => handlePlan(false)}
                  className='px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm'>
                  Plan project
                </button>
                <button onClick={() => handlePlan(true)}
                  className='px-4 py-2 rounded-lg bg-white/5 text-gray-300 hover:bg-white/10 text-sm'>
                  Plan from CLAUDE.md
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {tab === 'queue' && tickets.length > 0 && (
        <div className='grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4'>
          {[...lanes, ...extras].map(lane => (
            <div key={lane.status} className='space-y-2'>
              <div className='flex items-center justify-between px-1'>
                <p className='text-[10px] uppercase tracking-wider font-semibold text-gray-500'>
                  {LANE_LABEL[lane.status]}
                </p>
                <span className='text-[10px] text-gray-600 font-mono'>{lane.items.length}</span>
              </div>
              {lane.items.length === 0
                ? <p className='text-[11px] text-gray-700 italic px-1'>empty</p>
                : lane.items.map(t => (
                    <TicketCard key={t.id} ticket={t} busy={busy === t.id}
                      onMove={(tid, status) => act(tid, () => moveTicket(tid, status))}
                      onRun={tid => act(tid, () => runTicket(tid))}
                      onBudget={(tid, amount) => act(tid, () => setTicketBudget(tid, amount))}
                      onArea={(tid, area) => act(tid, () => setTicketArea(tid, area))} />
                  ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
