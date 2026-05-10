import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getProjects } from '../api/client';
import type { Project } from '../api/client';

interface ParsedTask {
  title: string;
  description: string;
}

interface PlanResult {
  tasks: ParsedTask[];
  summary: string;
}

type Mode = 'tasks' | 'architecture';

export default function Intake() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [markdownText, setMarkdownText] = useState('');
  const [fileName, setFileName] = useState('');
  const [mode, setMode] = useState<Mode>('tasks');
  const [phase, setPhase] = useState<'upload' | 'plan' | 'arch' | 'done'>('upload');

  // tasks mode
  const [plan, setPlan] = useState<PlanResult | null>(null);
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);
  const [savedCount, setSavedCount] = useState(0);

  // architecture mode
  const [archOutput, setArchOutput] = useState('');
  const [archStreaming, setArchStreaming] = useState(false);
  const [archSaved, setArchSaved] = useState(false);
  const [archSavedPath, setArchSavedPath] = useState('');

  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    getProjects().then(setProjects).catch(console.error);
  }, []);

  const selectedProject = projects.find(p => p.id.toString() === selectedProjectId) ?? null;

  const loadFile = (file: File) => {
    if (!file.name.endsWith('.md') && !file.name.endsWith('.txt')) {
      setError('Please upload a .md or .txt file.');
      return;
    }
    setError('');
    setFileName(file.name);
    const reader = new FileReader();
    reader.onload = (e) => setMarkdownText((e.target?.result as string) ?? '');
    reader.readAsText(file);
  };

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) loadFile(f);
  };

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) loadFile(f);
  }, []);

  const onDragOver = (e: React.DragEvent) => { e.preventDefault(); setDragging(true); };
  const onDragLeave = () => setDragging(false);

  // ── Tasks mode ──────────────────────────────────────────────────────────────

  const handleGeneratePlan = async () => {
    if (!markdownText.trim()) { setError('No content to parse.'); return; }
    setError('');
    setPhase('plan');
    try {
      const res = await fetch('/api/intake/parse-markdown', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: markdownText, project_id: selectedProjectId || null }),
      });
      if (!res.ok) throw new Error('Server error ' + res.status);
      const data: PlanResult = await res.json();
      setPlan(data);
      setSelectedTasks(new Set(data.tasks.map((_, i) => i)));
    } catch (e) {
      setError(String(e));
      setPhase('upload');
    }
  };

  const toggleTask = (i: number) => {
    setSelectedTasks(prev => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  };

  const handleSaveTasks = async () => {
    if (!plan || selectedTasks.size === 0) return;
    setSaving(true);
    setError('');
    const toSave = plan.tasks.filter((_, i) => selectedTasks.has(i));
    try {
      for (const task of toSave) {
        await fetch('/api/backlog', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: task.title,
            description: task.description,
            project_id: selectedProject ? selectedProject.id : null,
            project_path: selectedProject?.project_path ?? '',
            claude_md_path: selectedProject?.claude_md_path ?? '',
          }),
        });
      }
      setSavedCount(toSave.length);
      setPhase('done');
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  // ── Architecture mode ───────────────────────────────────────────────────────

  const handleGenerateArchitecture = async () => {
    if (!markdownText.trim()) { setError('No content to parse.'); return; }
    setError('');
    setArchOutput('');
    setArchSaved(false);
    setArchSavedPath('');
    setArchStreaming(true);
    setPhase('arch');

    try {
      const res = await fetch('/api/intake/generate-architecture', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: markdownText,
          project_name: selectedProject?.name ?? '',
          project_path: selectedProject?.project_path ?? '',
        }),
      });
      if (!res.ok || !res.body) throw new Error('Server error ' + res.status);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (payload === '[DONE]') break;
          try {
            const event = JSON.parse(payload);
            if (event.text) {
              accumulated += event.text;
              setArchOutput(accumulated);
            }
            if (event.error) setError(event.error);
          } catch {}
        }
      }
    } catch (e) {
      setError(String(e));
      setPhase('upload');
    } finally {
      setArchStreaming(false);
    }
  };

  const handleDownloadArchitecture = () => {
    const blob = new Blob([archOutput], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'ARCHITECTURE.md';
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleSaveArchitecture = async () => {
    if (!selectedProject?.project_path) { setError('Select a project to save directly.'); return; }
    try {
      const res = await fetch('/api/intake/save-architecture', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_path: selectedProject.project_path, content: archOutput }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error ?? 'Save failed'); return; }
      setArchSaved(true);
      setArchSavedPath(data.path);
    } catch (e) {
      setError(String(e));
    }
  };

  // ── Reset ───────────────────────────────────────────────────────────────────

  const handleReset = () => {
    setMarkdownText(''); setFileName(''); setPlan(null);
    setSelectedTasks(new Set()); setSavedCount(0); setError('');
    setArchOutput(''); setArchSaved(false); setArchSavedPath('');
    setPhase('upload');
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className='max-w-2xl mx-auto space-y-5'>
      <div className='flex items-center justify-between'>
        <h1 className='text-lg font-semibold text-white'>Intake</h1>
        {phase !== 'upload' && (
          <button onClick={handleReset} className='text-sm text-gray-400 active:text-white py-1'>
            ← Start over
          </button>
        )}
      </div>

      {error && (
        <div className='bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 text-sm text-red-300'>
          {error}
        </div>
      )}

      {/* PHASE: upload */}
      {phase === 'upload' && (
        <div className='space-y-4'>
          {/* Mode toggle */}
          <div className='flex rounded-lg overflow-hidden border border-white/10 text-sm'>
            <button
              onClick={() => setMode('tasks')}
              className={'flex-1 py-2.5 font-medium transition-colors ' +
                (mode === 'tasks' ? 'bg-violet-600 text-white' : 'text-gray-400 active:text-white')}
            >
              Extract Tasks
            </button>
            <button
              onClick={() => setMode('architecture')}
              className={'flex-1 py-2.5 font-medium transition-colors ' +
                (mode === 'architecture' ? 'bg-violet-600 text-white' : 'text-gray-400 active:text-white')}
            >
              Generate ARCHITECTURE.md
            </button>
          </div>

          <div>
            <label className='block text-sm text-gray-400 mb-1'>Project (optional)</label>
            <select value={selectedProjectId} onChange={e => setSelectedProjectId(e.target.value)}
              className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2.5 text-sm text-gray-200 focus:outline-none focus:border-violet-500'>
              <option value=''>No project</option>
              {projects.map(p => <option key={p.id} value={p.id.toString()}>{p.name}</option>)}
            </select>
          </div>

          <div>
            <input ref={fileInputRef} type='file' accept='.md,.txt' className='hidden' onChange={onFileChange} />
            <button
              onClick={() => fileInputRef.current?.click()}
              onDrop={onDrop} onDragOver={onDragOver} onDragLeave={onDragLeave}
              className={'w-full rounded-xl border-2 border-dashed py-6 px-4 text-center transition-colors ' +
                (dragging ? 'border-violet-400 bg-violet-500/10' : 'border-white/10 active:border-violet-400')}
            >
              <p className='text-sm text-gray-300'>
                {fileName
                  ? <span className='text-violet-300 font-medium'>{fileName}</span>
                  : <><span className='text-violet-300 font-medium'>Tap to browse</span> or drag a <span className='font-mono'>.md</span> file</>
                }
              </p>
            </button>
          </div>

          <div>
            <label className='block text-sm text-gray-400 mb-1'>Or paste markdown</label>
            <textarea value={markdownText} onChange={e => { setMarkdownText(e.target.value); setFileName(''); }}
              placeholder={'# My project plan\n\n## Overview\n...'}
              rows={6}
              className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2.5 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500 resize-none'
            />
          </div>

          <button
            onClick={mode === 'tasks' ? handleGeneratePlan : handleGenerateArchitecture}
            disabled={!markdownText.trim()}
            className='w-full py-3 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium disabled:opacity-40 transition-colors'
          >
            {mode === 'tasks' ? 'Generate Plan →' : 'Generate ARCHITECTURE.md →'}
          </button>
        </div>
      )}

      {/* PHASE: plan — loading */}
      {phase === 'plan' && !plan && (
        <div className='flex flex-col items-center justify-center py-16 gap-3 text-gray-400 text-sm'>
          <div className='w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin' />
          Parsing your plan...
        </div>
      )}

      {/* PHASE: plan — review */}
      {phase === 'plan' && plan && (
        <div className='space-y-4'>
          {plan.summary && (
            <div className='bg-white/5 rounded-xl px-4 py-3 text-sm text-gray-300 border border-white/5'>
              {plan.summary}
            </div>
          )}

          <div className='flex items-center justify-between'>
            <p className='text-sm text-gray-400'>{plan.tasks.length} tasks — tap to select</p>
            <button onClick={() => setSelectedTasks(
              selectedTasks.size === plan.tasks.length ? new Set() : new Set(plan.tasks.map((_, i) => i))
            )} className='text-xs text-violet-400 active:text-violet-200 py-1 px-2'>
              {selectedTasks.size === plan.tasks.length ? 'Deselect all' : 'Select all'}
            </button>
          </div>

          <div className='space-y-2'>
            {plan.tasks.map((task, i) => (
              <button key={i} onClick={() => toggleTask(i)}
                className={'w-full flex items-start gap-3 p-4 rounded-xl border text-left transition-colors ' +
                  (selectedTasks.has(i) ? 'bg-violet-600/10 border-violet-500/40' : 'bg-[#161927] border-white/5 active:border-white/20')}>
                <span className={'mt-0.5 w-5 h-5 flex-shrink-0 rounded border flex items-center justify-center text-xs ' +
                  (selectedTasks.has(i) ? 'bg-violet-600 border-violet-500 text-white' : 'border-white/20')}>
                  {selectedTasks.has(i) && '✓'}
                </span>
                <div className='flex-1 min-w-0'>
                  <p className='text-sm text-white leading-snug'>{task.title}</p>
                  {task.description && (
                    <p className='text-xs text-gray-400 mt-1 leading-relaxed'>{task.description}</p>
                  )}
                </div>
              </button>
            ))}
          </div>

          <button onClick={handleSaveTasks} disabled={saving || selectedTasks.size === 0}
            className='w-full py-3 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium disabled:opacity-40 transition-colors sticky bottom-24 sm:bottom-4 shadow-lg'>
            {saving ? 'Saving...' : `Add ${selectedTasks.size} task${selectedTasks.size !== 1 ? 's' : ''} to backlog`}
          </button>
        </div>
      )}

      {/* PHASE: architecture generation */}
      {phase === 'arch' && (
        <div className='space-y-4'>
          <div className='flex items-center justify-between'>
            <p className='text-sm text-gray-400'>
              {archStreaming
                ? <span className='flex items-center gap-2'><span className='w-3 h-3 border border-violet-500 border-t-transparent rounded-full animate-spin inline-block' /> Generating ARCHITECTURE.md...</span>
                : 'ARCHITECTURE.md ready'
              }
            </p>
            {!archStreaming && archOutput && (
              <span className='text-xs text-green-400'>✓ Complete</span>
            )}
          </div>

          {archOutput && (
            <pre className='bg-[#0d0f1a] border border-white/10 rounded-xl p-4 text-xs text-gray-300 overflow-auto max-h-[60vh] whitespace-pre-wrap font-mono leading-relaxed'>
              {archOutput}
            </pre>
          )}

          {!archStreaming && archOutput && (
            <div className='flex flex-col sm:flex-row gap-3'>
              <button onClick={handleDownloadArchitecture}
                className='flex-1 py-3 rounded-lg bg-white/10 active:bg-white/20 text-white text-sm font-medium transition-colors'>
                ↓ Download ARCHITECTURE.md
              </button>
              {selectedProject?.project_path && !archSaved && (
                <button onClick={handleSaveArchitecture}
                  className='flex-1 py-3 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium transition-colors'>
                  Save to {selectedProject.name}
                </button>
              )}
              {archSaved && (
                <div className='flex-1 py-3 rounded-lg bg-green-600/20 border border-green-500/30 text-green-300 text-sm text-center'>
                  ✓ Saved to project
                </div>
              )}
            </div>
          )}

          {archSaved && archSavedPath && (
            <p className='text-xs text-gray-500 font-mono'>{archSavedPath}</p>
          )}
        </div>
      )}

      {/* PHASE: done (tasks saved) */}
      {phase === 'done' && (
        <div className='text-center py-16 space-y-4'>
          <p className='text-3xl'>✓</p>
          <p className='text-white font-medium'>{savedCount} task{savedCount !== 1 ? 's' : ''} added to backlog</p>
          <div className='flex flex-col sm:flex-row gap-3 justify-center'>
            <a href='/backlog' className='px-4 py-3 rounded-lg bg-violet-600 text-white text-sm text-center'>
              View Backlog
            </a>
            <button onClick={handleReset} className='px-4 py-3 rounded-lg bg-white/5 text-gray-300 text-sm'>
              Add more
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
