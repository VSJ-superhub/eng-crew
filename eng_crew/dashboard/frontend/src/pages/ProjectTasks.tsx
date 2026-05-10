import React, { useEffect, useRef, useState, useCallback } from 'react';
import { useParams, Link, useLocation } from 'react-router-dom';
import { 
  getProject, getProjectTasks, getProjectRuns, addProjectTask, deleteProjectTask, runProjectTask,
  getProjectPlan, createProjectPlan, planFromClaudeMd, runPlanSprint, skipPlanSprint,
  getPlanSprintTasks, runSprintTask, getProjectArchitecture, updateArchitecture,
  getRunDetail, getProjectFeatures, Feature, PlanSprint, SprintTask, Project, BacklogItem, Run, ProjectStats, ProjectPlan
} from '../api/client';

const STATUS_COLOR: Record<string, string> = {
  pending: 'text-gray-400', running: 'text-blue-400 animate-pulse',
  done: 'text-green-400', completed: 'text-green-400',
  failed: 'text-red-400', paused: 'text-amber-400', skipped: 'text-gray-500',
};

const COMPLEXITY_BADGE: Record<string, string> = {
  low:    'bg-green-500/15 text-green-400',
  medium: 'bg-amber-500/15 text-amber-400',
  high:   'bg-red-500/15 text-red-400',
};

const SPRINT_STATUS_BADGE: Record<string, string> = {
  pending: 'bg-gray-500/20 text-gray-400',
  running: 'bg-blue-500/20 text-blue-300',
  done:    'bg-green-500/20 text-green-300',
  failed:  'bg-red-500/20 text-red-300',
  skipped: 'bg-gray-500/10 text-gray-500',
};

function ProgressBar({ progress, className = "" }: { progress: number, className?: string }) {
  return (
    <div className={`h-1.5 w-full bg-white/5 rounded-full overflow-hidden ${className}`}>
      <div 
        className="h-full bg-violet-500 transition-all duration-500" 
        style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
      />
    </div>
  );
}

function FeatureCard({ feature, onClick }: { feature: Feature, onClick: () => void }) {
  return (
    <div 
      onClick={onClick}
      className='bg-[#161927] rounded-xl p-5 border border-white/5 hover:border-violet-500/40 transition-all cursor-pointer group'
    >
      <div className='flex justify-between items-start mb-4'>
        <div>
          <h3 className='text-white font-medium group-hover:text-violet-300 transition-colors'>{feature.title}</h3>
          <p className='text-xs text-gray-500 mt-1 line-clamp-2'>{feature.description || 'No description provided.'}</p>
        </div>
        <span className='text-lg font-bold text-violet-400'>{Math.round(feature.progress)}%</span>
      </div>
      
      <div className='space-y-2'>
        <div className='flex justify-between text-[10px] uppercase tracking-wider font-semibold'>
          <span className='text-gray-500'>Sprints</span>
          <span className='text-gray-400'>{feature.done_sprints} / {feature.sprint_count}</span>
        </div>
        <ProgressBar progress={feature.progress} />
      </div>
      
      <div className='mt-4 flex items-center justify-between'>
        <span className={`text-[10px] px-2 py-0.5 rounded font-bold uppercase ${feature.status === 'done' ? 'bg-green-500/10 text-green-400' : 'bg-blue-500/10 text-blue-400'}`}>
          {feature.status}
        </span>
        <span className='text-xs text-gray-600 group-hover:text-gray-400 transition-colors'>View Roadmap →</span>
      </div>
    </div>
  );
}

function LiveSprintProgress({ runId }: { runId: number }) {
  const [plan, setPlan] = useState<any[]>([])
  const [runStatus, setRunStatus] = useState('')

  useEffect(() => {
    let active = true
    const load = () =>
      getRunDetail(runId)
        .then(d => {
          if (!active) return
          setPlan(d.plan ?? [])
          setRunStatus(d.run.status)
        })
        .catch(() => null)
    load()
    const iv = setInterval(load, 3000)
    return () => { active = false; clearInterval(iv) }
  }, [runId])

  if (plan.length === 0) return (
    <div className='px-4 pb-3 pt-2 border-t border-white/5'>
      <p className='text-xs text-gray-500 animate-pulse'>Planning subtasks...</p>
    </div>
  )

  const statusIcon: Record<string, string> = { done: '✓', completed: '✓', failed: '✗', running: '▶', pending: '○', awaiting_review: '👁' }
  const statusColor: Record<string, string> = { done: 'text-green-400', completed: 'text-green-400', failed: 'text-red-400', running: 'text-blue-400 animate-pulse', pending: 'text-gray-500', awaiting_review: 'text-violet-400' }
  const agColor: Record<string, string> = { frontend: 'text-blue-400', backend: 'text-green-400', database: 'text-orange-400', ai_pipeline: 'text-purple-400', infrastructure: 'text-yellow-400' }

  const doneCount = plan.filter(p => p.status === 'done' || p.status === 'completed').length;
  const progress = (doneCount / plan.length) * 100;

  return (
    <div className='px-4 pb-3 pt-3 border-t border-blue-500/20 bg-blue-500/3'>
      <div className='flex justify-between items-center mb-2'>
        <p className='text-[10px] text-blue-400 uppercase tracking-wider font-semibold'>Live execution</p>
        <span className='text-[10px] text-gray-400 font-mono'>{doneCount}/{plan.length} subtasks</span>
      </div>
      <ProgressBar progress={progress} className="mb-3 h-1" />
      <div className='space-y-1 max-h-32 overflow-y-auto pr-1'>
        {plan.map(st => (
          <div key={st.id} className='flex items-center gap-2'>
            <span className={`text-[10px] w-3 shrink-0 ${statusColor[st.status ?? 'pending'] ?? 'text-gray-500'}`}>
              {statusIcon[st.status ?? 'pending'] ?? '○'}
            </span>
            <span className={`text-[10px] font-mono w-14 shrink-0 ${agColor[st.agent_type] ?? 'text-gray-500'}`}>
              {st.agent_type?.replace('_', '')}
            </span>
            <span className={`text-[10px] flex-1 min-w-0 truncate ${st.status === 'done' || st.status === 'completed' ? 'text-gray-600' : 'text-gray-300'}`}>
              {st.description}
            </span>
          </div>
        ))}
      </div>
      <p className='text-[10px] text-gray-600 mt-2'>
        <Link to={`/run/${runId}`} className='hover:text-gray-400 underline'>View technical logs →</Link>
      </p>
    </div>
  )
}

function SprintCard({
  sprint, sprints, onRun, onSkip, running, isExpanded, onToggle
}: {
  sprint: PlanSprint
  sprints: PlanSprint[]
  onRun: (id: number) => void
  onSkip: (id: number) => void
  running: number | null
  isExpanded: boolean
  onToggle: () => void
}) {
  const [tasks, setTasks] = useState<SprintTask[]>([])
  const [tasksLoading, setTasksLoading] = useState(false)

  const blockers = sprint.depends_on
    .map(n => sprints.find(s => s.sprint_number === n))
    .filter((s): s is PlanSprint => !!s && s.status !== 'done' && s.status !== 'skipped')

  const canRun = (sprint.status === 'pending' || sprint.status === 'failed') && blockers.length === 0 && running === null
  const isRunning = running === sprint.id || sprint.status === 'running'

  useEffect(() => {
    if (isExpanded && tasks.length === 0) {
      setTasksLoading(true)
      getPlanSprintTasks(sprint.id)
        .then(data => setTasks(data.tasks))
        .catch(console.error)
        .finally(() => setTasksLoading(false))
    }
  }, [isExpanded, sprint.id, tasks.length])

  return (
    <div className={`bg-[#161927] rounded-xl border transition-all ${
      sprint.status === 'done' ? 'border-green-500/20' :
      sprint.status === 'running' ? 'border-blue-500/40 bg-blue-500/5' :
      sprint.status === 'failed' ? 'border-red-500/30' :
      'border-white/5'
    }`}>
      <div 
        className='flex items-center gap-4 p-4 cursor-pointer group'
        onClick={onToggle}
      >
        <div className={`shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${
          sprint.status === 'done' ? 'bg-green-500/20 text-green-400' :
          sprint.status === 'running' ? 'bg-blue-500/20 text-blue-400' :
          'bg-white/5 text-gray-500'
        }`}>
          {sprint.status === 'done' ? '✓' : sprint.sprint_number}
        </div>
        
        <div className='flex-1 min-w-0'>
          <div className='flex items-center gap-2 mb-0.5'>
            <h4 className='text-sm font-medium text-white group-hover:text-violet-300 transition-colors'>{sprint.name}</h4>
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold uppercase ${SPRINT_STATUS_BADGE[sprint.status] ?? ''}`}>
              {sprint.status}
            </span>
          </div>
          <p className='text-xs text-gray-500 truncate'>{sprint.description}</p>
        </div>

        <div className='flex items-center gap-3' onClick={e => e.stopPropagation()}>
          {sprint.status !== 'done' && sprint.status !== 'skipped' && (
            <button
              onClick={() => onRun(sprint.id)}
              disabled={!canRun || isRunning}
              className={`text-xs px-3 py-1.5 rounded font-bold transition-all ${
                isRunning ? 'bg-blue-500/20 text-blue-300 cursor-default' :
                canRun ? 'bg-violet-600 hover:bg-violet-500 text-white' :
                'bg-white/5 text-gray-600 cursor-not-allowed'
              }`}
            >
              {isRunning ? 'Running...' : blockers.length > 0 ? 'Blocked' : 'Start Sprint'}
            </button>
          )}
          <span className={`text-gray-600 transition-transform ${isExpanded ? 'rotate-180' : ''}`}>▾</span>
        </div>
      </div>

      {isRunning && sprint.run_id && <LiveSprintProgress runId={sprint.run_id} />}

      {isExpanded && (
        <div className='px-4 pb-4 pt-0 border-t border-white/5'>
          <div className='py-3'>
            <p className='text-xs text-gray-400 leading-relaxed'>{sprint.description}</p>
            {sprint.acceptance_criteria && (
              <div className='mt-3 p-2 bg-[#0f111a] rounded border border-white/5'>
                <p className='text-[10px] uppercase text-gray-500 font-bold mb-1'>Acceptance Criteria</p>
                <p className='text-xs text-gray-400'>{sprint.acceptance_criteria}</p>
              </div>
            )}
          </div>
          
          <div className='space-y-2 mt-2'>
            <p className='text-[10px] uppercase text-gray-500 font-bold'>Subtasks</p>
            {tasksLoading ? <p className='text-xs text-gray-600 animate-pulse'>Loading tasks...</p> : 
             tasks.length === 0 ? <p className='text-xs text-gray-700 italic'>No subtasks generated yet.</p> :
             tasks.map(t => (
               <div key={t.id} className='flex items-center gap-2 text-xs'>
                 <span className={STATUS_COLOR[t.status] || 'text-gray-500'}>
                   {t.status === 'done' || t.status === 'completed' ? '✓' : '○'}
                 </span>
                 <span className={t.status === 'done' || t.status === 'completed' ? 'text-gray-600' : 'text-gray-300'}>
                   {t.title}
                 </span>
               </div>
             ))
            }
          </div>
        </div>
      )}
    </div>
  );
}

export default function ProjectTasks() {
  const { id } = useParams<{ id: string }>()
  const [project, setProject] = useState<Project | null>(null)
  const [features, setFeatures] = useState<Feature[]>([])
  const [selectedFeature, setSelectedFeature] = useState<Feature | null>(null)
  const [sprints, setSprints] = useState<PlanSprint[]>([])
  const [expandedSprint, setExpandedSprint] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [planning, setPlanning] = useState(false)
  const [planningFailed, setPlanningFailed] = useState(false)
  const [planCreatedAt, setPlanCreatedAt] = useState<string | null>(null)
  const [runningSprint, setRunningSprint] = useState<number | null>(null)
  
  const load = useCallback(async () => {
    if (!id) return
    try {
      const [proj, feats] = await Promise.all([
        getProject(parseInt(id)),
        getProjectFeatures(parseInt(id))
      ])
      setProject(proj)
      setFeatures(feats.features)
      
      // If there's exactly one feature/roadmap item, or if there are none (direct sprints),
      // we might want to auto-select it. But for now, if no features, 
      // let's ensure we load sprints so we can show a roadmap if one exists.
      if (feats.features.length === 0) {
        const data = await getProjectPlan(parseInt(id))
        setSprints(data.sprints)
        setPlanning(data.planning)
        setPlanningFailed(!!(data as any).planning_failed)
        setPlanCreatedAt(data.plan?.created_at ?? null)
      }
    } catch (e) { 
      console.error(e)
    } finally {
      setLoading(false)
    }
  }, [id])

  const loadSprints = useCallback(async () => {
    if (!id) return
    try {
      const data = await getProjectPlan(parseInt(id))
      setSprints(data.sprints)
      setPlanning(data.planning)
      setPlanningFailed(!!(data as any).planning_failed)
      setPlanCreatedAt(data.plan?.created_at ?? null)
    } catch (e) { console.error(e) }
  }, [id])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (selectedFeature) loadSprints()
  }, [selectedFeature, loadSprints])

  // Poll while planning is in progress so elapsed time stays fresh and we catch completion
  useEffect(() => {
    if (!planning) return
    const iv = setInterval(loadSprints, 5000)
    return () => clearInterval(iv)
  }, [planning, loadSprints])

  const handleRunSprint = async (sprintId: number) => {
    setRunningSprint(sprintId)
    try {
      await runPlanSprint(sprintId)
      const iv = setInterval(async () => {
        const data = await getProjectPlan(parseInt(id!))
        setSprints(data.sprints)
        const s = data.sprints.find(x => x.id === sprintId)
        if (s && s.status !== 'running') { clearInterval(iv); setRunningSprint(null) }
      }, 3000)
    } catch (e) { console.error(e); setRunningSprint(null) }
  }

  if (loading) return <div className='flex items-center justify-center h-64 text-gray-500 animate-pulse'>Loading project data...</div>
  if (!project) return <div className='flex items-center justify-center h-64 text-red-400'>Project not found.</div>

  const showGallery = !selectedFeature && features.length > 0;

  return (
    <div className='p-6 max-w-6xl mx-auto'>
      {/* Breadcrumbs */}
      <nav className='flex items-center gap-2 text-sm mb-6'>
        <Link to='/projects' className='text-gray-500 hover:text-white transition-colors'>Projects</Link>
        <span className='text-gray-700'>/</span>
        <button 
          onClick={() => { setSelectedFeature(null); setExpandedSprint(null) }}
          className={`font-medium transition-colors ${selectedFeature ? 'text-gray-500 hover:text-white' : 'text-white'}`}
        >
          {project.name}
        </button>
        {selectedFeature && (
          <>
            <span className='text-gray-700'>/</span>
            <span className='text-white font-medium max-w-[200px] truncate' title={selectedFeature.title}>
              {selectedFeature.title}
            </span>
          </>
        )}
      </nav>

      {/* Header */}
      <div className='mb-8'>
        <h1 className='text-3xl font-bold text-white mb-2 line-clamp-2'>
          {selectedFeature ? selectedFeature.title : project.name}
        </h1>
        <p className='text-gray-400 text-sm max-w-2xl'>
          {selectedFeature ? selectedFeature.description : project.description || `Manage and track progress for ${project.name}.`}
        </p>
      </div>

      {showGallery ? (
        /* Level 1: Feature Gallery */
        <div className='grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6'>
          {features.map(f => (
            <FeatureCard key={f.id} feature={f} onClick={() => setSelectedFeature(f)} />
          ))}
        </div>
      ) : (
        /* Level 2: Sprint Roadmap (either for a selected feature or direct project sprints) */
        <div className='space-y-6'>
          <div className='flex justify-between items-center mb-4'>
            <h2 className='text-lg font-semibold text-white flex items-center gap-2'>
              Roadmap Progress
              <span className='text-violet-400 text-sm font-mono'>
                ({Math.round(selectedFeature?.progress || (sprints.length > 0 ? (sprints.filter(s => s.status === 'done').length / sprints.length * 100) : 0))}%)
              </span>
            </h2>
            {features.length > 0 && (
              <button 
                onClick={() => setSelectedFeature(null)}
                className='text-xs text-gray-500 hover:text-gray-300 transition-colors'
              >
                ← Back to Features
              </button>
            )}
          </div>

          <ProgressBar progress={selectedFeature?.progress || (sprints.length > 0 ? (sprints.filter(s => s.status === 'done').length / sprints.length * 100) : 0)} className="h-2 mb-8" />

          {planningFailed ? (
            <div className='p-10 text-center bg-[#161927] rounded-2xl border border-red-500/30'>
              <p className='text-red-400 font-medium mb-1'>Roadmap generation failed</p>
              <p className='text-xs text-gray-500 mb-4'>The planner did not produce any sprints.</p>
              <button
                onClick={async () => {
                  setPlanningFailed(false)
                  setPlanning(true)
                  try {
                    await createProjectPlan(parseInt(id!), project?.description || project?.name || 'Plan this project', true)
                    loadSprints()
                  } catch (e) { console.error(e); setPlanning(false) }
                }}
                className='text-xs px-4 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded font-bold transition-all'
              >
                Retry
              </button>
            </div>
          ) : planning ? (
            <div className='p-10 text-center bg-[#161927] rounded-2xl border border-blue-500/20'>
              <div className='w-6 h-6 border-2 border-blue-400 border-t-transparent rounded-full animate-spin mx-auto mb-4' />
              <p className='text-white font-medium'>Generating Roadmap...</p>
              <p className='text-xs text-gray-500 mt-1'>The planner is breaking this down into logical sprints.</p>
              {planCreatedAt && (() => {
                const elapsed = Math.floor((Date.now() - new Date(planCreatedAt).getTime()) / 1000)
                const mins = Math.floor(elapsed / 60)
                const secs = elapsed % 60
                return <p className='text-xs text-gray-600 mt-1'>{mins > 0 ? `${mins}m ` : ''}{secs}s elapsed</p>
              })()}
            </div>
          ) : sprints.length > 0 ? (
            <div className='space-y-3'>
              {sprints.map(s => (
                <SprintCard 
                  key={s.id} 
                  sprint={s} 
                  sprints={sprints}
                  onRun={handleRunSprint}
                  onSkip={(sid) => skipPlanSprint(sid).then(loadSprints)}
                  running={runningSprint}
                  isExpanded={expandedSprint === s.id}
                  onToggle={() => setExpandedSprint(expandedSprint === s.id ? null : s.id)}
                />
              ))}
            </div>
          ) : (
            <div className='col-span-full py-20 text-center bg-[#161927] rounded-2xl border border-dashed border-white/10'>
              <p className='text-gray-500'>No features or plans found. Start by creating a plan in the Architecture tab or via CLI.</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
