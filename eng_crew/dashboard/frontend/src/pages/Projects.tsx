import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getProjects, addProject, browseFs, scanProject } from '../api/client';
import type { Project, FsBrowseResult } from '../api/client';

export default function Projects() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);

  // Browser state
  const [browseResult, setBrowseResult] = useState<FsBrowseResult | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);

  // Form state
  const [selectedPath, setSelectedPath] = useState('');
  const [manualPath, setManualPath] = useState('');
  const [projectName, setProjectName] = useState('');
  const [claudeMdPath, setClaudeMdPath] = useState('');
  const [scanning, setScanning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const load = async () => {
    try { setProjects(await getProjects()) }
    catch (e) { console.error(e) }
    finally { setLoading(false) }
  };

  useEffect(() => { load() }, []);

  const browse = async (path?: string) => {
    setBrowseLoading(true);
    try {
      const r = await browseFs(path ?? ''); // empty = backend defaults to ~/Projects
      setBrowseResult(r);
    } catch (e) { console.error(e) }
    finally { setBrowseLoading(false) }
  };

  const openAdd = () => {
    setSelectedPath('');
    setManualPath('');
    setProjectName('');
    setClaudeMdPath('');
    setError('');
    setShowAdd(true);
    browse(); // load ~/Projects on open
  };

  const handleSelectDir = async (path: string) => {
    setSelectedPath(path);
    setManualPath(path);
    setScanning(true);
    setError('');
    try {
      const r = await scanProject(path);
      if (r.name) setProjectName(r.name);
      if (r.claude_md_path) setClaudeMdPath(r.claude_md_path);
    } catch (e) { console.error(e) }
    finally { setScanning(false) }
  };

  const handleManualPath = async () => {
    const p = manualPath.trim();
    if (!p) return;
    // Try to browse to it first; if valid dir, select it
    try {
      await browseFs(p);
      await handleSelectDir(p);
    } catch {
      setError('Path not found or not a directory');
    }
  };

  const handleAdd = async () => {
    if (!selectedPath || !projectName) return;
    setSaving(true);
    setError('');
    // If no context file detected, default to ARCHITECTURE.md in the project folder
    const effectiveClaudeMd = claudeMdPath.trim() || (selectedPath.replace(/\\/g, '/') + '/ARCHITECTURE.md');
    try {
      const result = await addProject(projectName, selectedPath, effectiveClaudeMd);
      setShowAdd(false);
      navigate('/projects/' + result.id + '/tasks', { state: { tab: 'plan' } });
    } catch (e: any) {
      setError(e?.message || 'Failed to add project');
    } finally { setSaving(false) }
  };

  return (
    <div className='p-6 space-y-6'>
      <div className='flex items-center justify-between'>
        <h1 className='text-xl font-semibold text-white'>Projects</h1>
        <button onClick={openAdd}
          className='px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm'>
          + Add Project
        </button>
      </div>

      {loading && <p className='text-gray-400'>Loading...</p>}

      <div className='grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4'>
        {projects.map((p) => {
          const s = p.stats;
          const successRate = s && s.total_runs > 0
            ? Math.round((s.completed / s.total_runs) * 100) + '%' : null;
          return (
            <div key={p.id}
              onClick={() => navigate('/projects/' + p.id + '/tasks', { state: { tab: 'plan' } })}
              className='bg-[#161927] rounded-xl p-5 border border-white/5 cursor-pointer hover:border-violet-500/40 transition-colors'
            >
              <h2 className='text-base font-semibold text-white mb-1'>{p.name}</h2>
              <p className='text-xs text-gray-500 font-mono truncate mb-3'>{p.project_path}</p>
              {s && s.total_runs > 0 && (
                <div className='flex items-center gap-3 text-xs text-gray-500 border-t border-white/5 pt-3'>
                  <span>{s.total_runs} run{s.total_runs !== 1 ? 's' : ''}</span>
                  {successRate && <span className='text-green-400'>{successRate} success</span>}
                  <span className='ml-auto'>${(s.total_cost_usd ?? 0).toFixed(3)}</span>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Add Project Modal — div overlay, mobile-safe */}
      {showAdd && (
        <div className='fixed inset-0 z-50 flex items-end sm:items-center justify-center p-0 sm:p-4 bg-black/70'
          onClick={(e) => { if (e.target === e.currentTarget) setShowAdd(false) }}>
          <div className='bg-[#161927] w-full sm:max-w-lg rounded-t-2xl sm:rounded-2xl border border-white/10 flex flex-col max-h-[90vh]'>

            {/* Header */}
            <div className='flex items-center justify-between px-5 pt-5 pb-3 shrink-0'>
              <h2 className='text-base font-semibold text-white'>Add Project</h2>
              <button onClick={() => setShowAdd(false)}
                className='text-gray-400 hover:text-white text-xl leading-none'>×</button>
            </div>

            <div className='overflow-y-auto flex-1 px-5 pb-5 space-y-4'>

              {/* Path input with Go button */}
              <div>
                <p className='text-xs text-gray-400 mb-1.5'>Type or paste a path, or browse below</p>
                <div className='flex gap-2'>
                  <input
                    value={manualPath}
                    onChange={e => setManualPath(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleManualPath() }}
                    placeholder='e.g. C:/Users/you/Projects/myapp'
                    className='flex-1 min-w-0 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500/50'
                  />
                  <button onClick={handleManualPath}
                    className='px-3 py-2 rounded-lg bg-white/10 hover:bg-white/15 text-white text-sm shrink-0'>
                    Go
                  </button>
                </div>
                {error && <p className='text-xs text-red-400 mt-1'>{error}</p>}
              </div>

              {/* File browser */}
              <div>
                <div className='flex items-center justify-between mb-1.5'>
                  <p className='text-xs text-gray-400'>Browse</p>
                  {browseResult?.parent && (
                    <button onClick={() => browse(browseResult.parent!)}
                      className='text-xs text-gray-400 hover:text-white flex items-center gap-1'>
                      ← Up
                    </button>
                  )}
                </div>
                <p className='text-xs text-gray-600 font-mono mb-2 truncate'>{browseResult?.current ?? '…'}</p>

                <div className='bg-black/30 rounded-lg divide-y divide-white/5 max-h-52 overflow-y-auto'>
                  {browseLoading && (
                    <p className='text-xs text-gray-500 px-3 py-2'>Loading...</p>
                  )}
                  {!browseLoading && browseResult?.dirs.length === 0 && (
                    <p className='text-xs text-gray-500 px-3 py-2'>No subdirectories</p>
                  )}
                  {browseResult?.dirs.map((d) => (
                    <div key={d.path} className='flex items-center gap-2 px-3 py-2.5'>
                      <button onClick={() => browse(d.path)}
                        className='text-sm text-gray-200 hover:text-white flex-1 text-left truncate'>
                        📁 {d.name}
                      </button>
                      <button onClick={() => handleSelectDir(d.path)}
                        className='text-xs px-2.5 py-1 rounded bg-violet-600/80 hover:bg-violet-500 text-white shrink-0'>
                        Select
                      </button>
                    </div>
                  ))}
                </div>
              </div>

              {/* Selected path + auto-filled fields */}
              {selectedPath && (
                <div className='rounded-lg bg-green-500/10 border border-green-500/20 px-3 py-2'>
                  <p className='text-xs text-green-400 font-medium mb-0.5'>Selected</p>
                  <p className='text-xs text-gray-300 font-mono break-all'>{selectedPath}</p>
                  {scanning && <p className='text-xs text-gray-500 mt-1'>Scanning project...</p>}
                </div>
              )}

              <div className='space-y-3'>
                <input value={projectName} onChange={(e) => setProjectName(e.target.value)}
                  placeholder='Project name'
                  className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500/50'
                />
                <input value={claudeMdPath} onChange={(e) => setClaudeMdPath(e.target.value)}
                  placeholder='ARCHITECTURE.md path (auto-detected)'
                  className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500/50'
                />
              </div>
            </div>

            {/* Footer */}
            <div className='flex justify-end gap-2 px-5 py-4 border-t border-white/5 shrink-0'>
              <button onClick={() => setShowAdd(false)}
                className='px-4 py-2 rounded-lg bg-white/5 text-gray-300 hover:bg-white/10 text-sm'>
                Cancel
              </button>
              <button onClick={handleAdd}
                disabled={!selectedPath || !projectName || scanning || saving}
                className='px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm disabled:opacity-40'>
                {saving ? 'Adding...' : 'Add Project'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
