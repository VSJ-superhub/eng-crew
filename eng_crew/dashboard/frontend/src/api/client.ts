export class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, body: string) {
    super('API ' + status + ': ' + body)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export interface Run { id: number; task_text: string; status: string; started_at: string; ended_at?: string; duration_secs?: number; subtask_count?: number; cost_usd?: number; project_path?: string; git_branch?: string; total_subtasks?: number; done_subtasks?: number; current_agent?: string; current_subtask_idx?: number; current_subtask_desc?: string; running_cost?: number }
export interface RunEvent { id: number; event_type: string; agent: string; message: string; created_at: string; cost_usd?: number; tokens_used?: number; model?: string; preprocessor_details?: Record<string, unknown> }
export interface CostByAgentEntry { cost_usd: number; input_tokens: number; output_tokens: number; efficiency: number | string }
export interface SubtaskPlan { id: string; description: string; agent_type: string; target_files: string[]; status?: string; review_passed?: boolean; diff?: string }
export interface RunDetail { id: number; task_text: string; status: string; started_at: string; ended_at?: string; duration_secs?: number; cost_usd?: number; project_path?: string; git_branch?: string; final_summary?: string }

export interface Clarification {
  subtask_id: string
  question: string
  options?: string[]
}

export interface RunDetailResponse {
  run: RunDetail
  events: RunEvent[]
  plan: SubtaskPlan[]
  cost_by_agent: Record<string, CostByAgentEntry>
  plan_summary: string
  clarification?: Clarification
}
export interface BacklogItem { id: number; title: string; description?: string; project_path?: string; claude_md_path?: string; priority: number; status: string; created_at: string; project_id?: number; type?: string }
export interface ProjectStats { total_runs: number; completed: number; failed: number; running: number; total_cost_usd: number }
export interface Project { id: number; name: string; project_path: string; claude_md_path: string; repo_url?: string; default_branch?: string; tech_stack?: string[]; test_command?: string; description?: string; active?: number; created_at?: string; stats?: ProjectStats }
export interface FsEntry { name: string; path: string }
export interface FsBrowseResult { current: string; parent?: string; dirs: FsEntry[]; files?: (FsEntry & { steering: boolean })[] }
export interface ProjectScanResult { name: string; tech_stack: string[]; claude_md_path: string; has_claude_md: boolean; test_command: string; is_git_repo: boolean; default_branch: string }

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json', ...opts?.headers }, ...opts })
  if (!res.ok) { const text = await res.text().catch(() => res.statusText); throw new ApiError(res.status, text) }
  return res.json() as Promise<T>
}
function post<T>(p: string, b: unknown): Promise<T> { return api<T>(p, { method: 'POST', body: JSON.stringify(b) }) }
function put<T>(p: string, b: unknown): Promise<T> { return api<T>(p, { method: 'PUT', body: JSON.stringify(b) }) }
function del<T>(p: string): Promise<T> { return api<T>(p, { method: 'DELETE' }) }
export function getStatus(): Promise<{ active_runs: Run[]; recent_runs: Run[]; cost_by_model: Record<string, number> }> { return api('/api/status') }
export function getEntrolyStatus(): Promise<{ enabled: boolean; available: boolean; quality: string; token_budget: number }> { return api('/api/entroly/status') }
export interface StackDetail { description: string; agents: Record<string, { provider: string; model: string }> }
export interface StacksResponse {
  active: string;
  ollama_available: boolean;
  custom_overrides: Record<string, { provider: string; model: string }>;
  effective: Record<string, { provider: string; model: string }>;
  available_models: Record<string, string[]>;
  stacks: Record<string, StackDetail>;
}
export function getStacks(): Promise<StacksResponse> { return api('/api/stacks') }
export function setStack(stack: string): Promise<{ ok: boolean; stack: string }> { return post('/api/session/stack', { stack }) }
export function setAgentOverrides(overrides: Record<string, { provider: string; model: string }>): Promise<{ ok: boolean }> { return post('/api/session/agent-overrides', { overrides }) }
export function getRunDetail(id: number): Promise<RunDetailResponse> { return api('/api/run/' + id) }
export function getAwaitingApproval(): Promise<number[]> { return api('/api/runs/awaiting-approval') }
export function approveRun(id: number, p: { approved: boolean; feedback?: string; selected_task_ids?: string[]; task_comments?: Record<string,string> }): Promise<{ ok: boolean }> { return post('/api/runs/' + id + '/approve', p) }
export interface SubtaskReview { id: number; run_id: number; subtask_id: string; description: string; agent_type: string; target_files: string; exec_summary: string; tests_passed: number; status: string; created_at: string }
export function getAwaitingSubtaskReview(): Promise<{ run_ids: number[]; reviews: SubtaskReview[] }> { return api('/api/runs/awaiting-subtask-review') }
export function resolveSubtaskReview(run_id: number, approved: boolean): Promise<{ ok: boolean }> { return post('/api/runs/' + run_id + '/subtask-review', { approved }) }
export function respondClarification(run_id: number, answer: string): Promise<{ ok: boolean }> { return post('/api/run/' + run_id + '/clarify', { answer }) }
export function retryRun(id: number): Promise<{ ok: boolean }> { return post('/api/runs/' + id + '/retry', {}) }
export function cancelRun(id: number): Promise<{ ok: boolean }> { return post('/api/runs/' + id + '/cancel', {}) }
export function pauseRun(id: number): Promise<{ ok: boolean }> { return post('/api/runs/' + id + '/pause', {}) }
export function resumeRun(id: number): Promise<{ ok: boolean }> { return post('/api/runs/' + id + '/resume', {}) }
export function getBacklog(status?: string, project?: string): Promise<BacklogItem[]> {
  const p = new URLSearchParams()
  if (status && status !== 'all') p.set('status', status)
  if (project) p.set('project', project)
  return api('/api/backlog' + (p.toString() ? '?' + p.toString() : ''))
}
export function createBacklogItem(i: { title: string; description?: string; project_path?: string; claude_md_path?: string; priority?: number; project_id?: number; item_type?: string }): Promise<{ id: number }> { return post('/api/backlog', i) }
export function updateBacklogItem(id: number, f: Partial<BacklogItem>): Promise<{ ok: boolean }> { return put('/api/backlog/' + id, f) }
export function deleteBacklogItem(id: number): Promise<{ ok: boolean }> { return del('/api/backlog/' + id) }
export function runBacklogItem(id: number): Promise<{ ok: boolean }> { return post('/api/backlog/' + id + '/run', {}) }
export function getProjects(): Promise<Project[]> { return api('/api/projects') }
export function createProject(i: { name: string; project_path: string; claude_md_path: string; repo_url?: string; tech_stack?: string[]; test_command?: string; description?: string }): Promise<{ id: number }> { return post('/api/projects', i) }
export function getProject(id: number): Promise<Project> { return api('/api/projects/' + id) }
export function updateProject(id: number, f: Partial<Project>): Promise<{ ok: boolean }> { return put('/api/projects/' + id, f) }
export function deleteProject(id: number): Promise<{ ok: boolean }> { return del('/api/projects/' + id) }
export function getProjectTasks(id: number, status?: string): Promise<{ project: Project; tasks: BacklogItem[] }> {
  const p = new URLSearchParams()
  if (status && status !== 'all') p.set('status', status)
  return api('/api/projects/' + id + '/tasks' + (p.toString() ? '?' + p.toString() : ''))
}
export function getProjectTaskSummary(): Promise<unknown[]> { return api('/api/projects/task-summary') }
export function getProjectRuns(id: number): Promise<{ runs: Run[]; stats: ProjectStats }> { return api('/api/projects/' + id + '/runs') }
export function fsBrowse(path?: string, files?: boolean): Promise<FsBrowseResult> {
  const p = new URLSearchParams()
  if (path) p.set('path', path)
  if (files) p.set('files', 'true')
  return api('/api/fs/browse' + (p.toString() ? '?' + p.toString() : ''))
}
export function fsScan(path: string): Promise<ProjectScanResult> { return post('/api/fs/scan', { path }) }
export function fsReadFile(path: string): Promise<{ content: string }> { return api('/api/fs/read-file?path=' + encodeURIComponent(path)) }
export function fsWriteClaudeMd(path: string, content: string): Promise<{ ok: boolean; path: string }> { return post('/api/fs/write-claude-md', { path, content }) }
export function intakeExtract(history: { role: string; content: string }[]): Promise<{ title: string; description: string; tech_stack: string[] }> { return post('/api/intake/extract', { history }) }

type TextCb = (text: string) => void
type VoidCb = () => void
type ErrCb = (err: string) => void

export async function intakeChatStream(
  payload: { message: string; history: { role: string; content: string }[]; project_name?: string; project_description?: string; tech_stack?: string[] },
  onChunk: TextCb, onDone: VoidCb, onError: ErrCb
): Promise<void> {
  try {
    const res = await fetch('/api/intake/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    if (!res.ok || !res.body) { onError('failed ' + res.status); return }
    await readSSEStream(res.body, onChunk, onDone, onError)
  } catch (e) { onError(String(e)) }
}

export async function generateClaudeMdStream(
  payload: { project_name: string; project_path: string; tech_stack?: string[]; description?: string; notes?: string },
  onChunk: TextCb, onDone: VoidCb, onError: ErrCb
): Promise<void> {
  try {
    const res = await fetch('/api/intake/generate-claude-md', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    if (!res.ok || !res.body) { onError('failed ' + res.status); return }
    await readSSEStream(res.body, onChunk, onDone, onError)
  } catch (e) { onError(String(e)) }
}

async function readSSEStream(
  body: ReadableStream<Uint8Array>, onChunk: TextCb, onDone: VoidCb, onError: ErrCb
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const raw = line.slice(6).trim()
      if (raw === '[DONE]') { onDone(); return }
      try {
        const obj = JSON.parse(raw)
        if (obj.text) onChunk(obj.text)
        if (obj.error) onError(obj.error)
      } catch {}
    }
  }
  onDone()
}

export async function retrySubtask(runId: string, subtaskId: string | number): Promise<void> {
  await fetch('/api/runs/' + runId + '/retry', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subtask_id: subtaskId }),
  });
}

export async function addBacklogItem(title: string, projectPath: string, itemType: string = 'feature'): Promise<void> {
  await fetch('/api/backlog', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, project_path: projectPath, item_type: itemType }),
  });
}

export async function addProject(name: string, projectPath: string, claudeMdPath: string): Promise<{ id: number }> {
  const res = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, project_path: projectPath, claude_md_path: claudeMdPath }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Failed to add project');
  return data as { id: number };
}

export async function browseFs(path: string): Promise<FsBrowseResult> {
  const r = await fetch('/api/fs/browse?path=' + encodeURIComponent(path));
  return r.json();
}

export async function scanProject(path: string): Promise<ProjectScanResult> {
  const r = await fetch('/api/fs/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  return r.json();
}

export async function addProjectTask(projectId: string, title: string, projectPath: string, claudeMdPath: string, itemType: string = 'feature'): Promise<void> {
  await fetch('/api/backlog', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, project_id: parseInt(projectId), project_path: projectPath, claude_md_path: claudeMdPath, item_type: itemType }),
  });
}

export async function deleteProjectTask(taskId: number): Promise<void> {
  await fetch('/api/backlog/' + taskId, { method: 'DELETE' });
}

export async function runProjectTask(taskId: number): Promise<void> {
  await fetch('/api/backlog/' + taskId + '/run', { method: 'POST' });
}

export async function intakeChat(
  history: {role: string; content: string}[],
  projectId: string,
  onChunk: TextCb, onDone: VoidCb, onError: ErrCb
): Promise<void> {
  const lastMsg = history[history.length - 1]
  return intakeChatStream({
    message: lastMsg?.content ?? '',
    history: history.slice(0, -1),
  }, onChunk, onDone, onError)
}

export async function intakeGenerateClaudeMd(messages: Array<{role:string,content:string}>, projectId: string): Promise<{content?: string}> {
  const r = await fetch('/api/intake/generate-claude-md', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, project_id: projectId }),
  });
  return r.json();
}

export interface PlanSprint {
  id: number
  plan_id: number
  sprint_number: number
  name: string
  description: string
  rationale?: string
  status: string
  depends_on: number[]
  acceptance_criteria?: string
  scope_hints?: string[]
  complexity?: string
  risk_flags?: string[]
  run_id?: number
  created_at?: string
}

export interface ProjectPlan {
  id: number
  project_id: number
  goal: string
  status: string
  review_result?: string
  created_at?: string
}

export function getProjectPlan(projectId: number): Promise<{ plan: ProjectPlan | null; sprints: PlanSprint[]; planning: boolean }> {
  return api('/api/projects/' + projectId + '/plan')
}

export function createProjectPlan(projectId: number, goal: string, force = false): Promise<{ plan_id: number; sprints: PlanSprint[]; existing?: boolean }> {
  return post('/api/projects/' + projectId + '/plan', { goal, force })
}

export function getProjectArchitecture(projectId: number): Promise<{ content: string | null; path: string; filename: string; error?: string }> {
  return api('/api/projects/' + projectId + '/architecture')
}
export function planFromClaudeMd(projectId: number): Promise<{ plan_id: number; sprints: PlanSprint[]; existing?: boolean }> {
  return post('/api/projects/' + projectId + '/plan-from-claude-md', {})
}

export function runPlanSprint(planSprintId: number): Promise<{ ok: boolean }> {
  return post('/api/plan-sprints/' + planSprintId + '/run', {})
}

export async function updateArchitecture(
  projectId: number,
  onChunk: (text: string) => void,
  onDone: () => void,
  onError: (err: string) => void
): Promise<void> {
  try {
    const res = await fetch('/api/projects/' + projectId + '/update-architecture', { method: 'POST' })
    if (!res.ok || !res.body) { onError('failed ' + res.status); return }
    await readSSEStream(res.body, onChunk, onDone, onError)
  } catch (e) { onError(String(e)) }
}

export function skipPlanSprint(planSprintId: number): Promise<{ ok: boolean }> {
  return put('/api/plan-sprints/' + planSprintId, { status: 'skipped' })
}

export interface SprintTask {
  id: number
  title: string
  description?: string
  status: string
  type?: string
  agent_type?: string
}

export interface Feature {
  id: number;
  title: string;
  description?: string;
  status: string;
  project_id: number;
  progress: number;
  sprint_count: number;
  done_sprints: number;
}

export function getProjectFeatures(projectId: number): Promise<{ features: Feature[] }> {
  return api('/api/projects/' + projectId + '/features')
}

export function getPlanSprintTasks(planSprintId: number): Promise<{ tasks: SprintTask[] }> {
  return api('/api/plan-sprints/' + planSprintId + '/tasks')
}

export function runSprintTask(taskId: number): Promise<{ ok: boolean }> {
  return post('/api/backlog/' + taskId + '/run', {})
}
