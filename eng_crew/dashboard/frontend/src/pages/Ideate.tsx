import React, { useEffect, useRef, useState } from 'react';
import { getProjects } from '../api/client';
import type { Project } from '../api/client';

interface Msg {
  role: 'user' | 'manager';
  content: string;
}

interface Proposal {
  task: string;
  rationale: string;
}

interface DispatchedBuild {
  run_id: number;
  task: string;
}

export default function Ideate() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [dispatching, setDispatching] = useState(false);
  const [builds, setBuilds] = useState<DispatchedBuild[]>([]);
  const [error, setError] = useState('');
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getProjects().then(setProjects).catch(console.error);
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, thinking, proposal, builds]);

  const selectedProject = projects.find(p => p.id.toString() === selectedProjectId) ?? null;

  const send = async () => {
    const text = input.trim();
    if (!text || thinking) return;
    if (!selectedProject?.project_path) {
      setError('Pick a project first — the manager ideates against its real code.');
      return;
    }
    setError('');
    setProposal(null);
    const history = messages.map(m => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
    }));
    setMessages(prev => [...prev, { role: 'user', content: text }]);
    setInput('');
    setThinking(true);
    try {
      const res = await fetch('/api/manager/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history,
          project_path: selectedProject.project_path,
          project_context: selectedProject.name ?? '',
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Server error ' + res.status);
      setMessages(prev => [...prev, { role: 'manager', content: data.reply }]);
      if (data.proposal?.task) setProposal(data.proposal);
    } catch (e) {
      setError(String(e));
    } finally {
      setThinking(false);
    }
  };

  const build = async () => {
    if (!proposal || !selectedProject?.project_path) return;
    setDispatching(true);
    setError('');
    try {
      const res = await fetch('/api/manager/dispatch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task: proposal.task, project_path: selectedProject.project_path }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Dispatch failed');
      setBuilds(prev => [...prev, { run_id: data.run_id, task: proposal.task }]);
      setMessages(prev => [
        ...prev,
        { role: 'manager', content: `🚀 Dispatched build #${data.run_id}. I'll build it on an isolated branch — track it on the dashboard.` },
      ]);
      setProposal(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setDispatching(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const reset = () => {
    setMessages([]); setProposal(null); setBuilds([]); setError(''); setInput('');
  };

  return (
    <div className='max-w-2xl mx-auto flex flex-col h-full'>
      <div className='flex items-center justify-between mb-3'>
        <div>
          <h1 className='text-lg font-semibold text-white'>Ideate</h1>
          <p className='text-xs text-gray-500'>Talk through an idea — the manager grounds it in your code, then builds it.</p>
        </div>
        {messages.length > 0 && (
          <button onClick={reset} className='text-sm text-gray-400 active:text-white py-1'>← New</button>
        )}
      </div>

      <div className='mb-3'>
        <select
          value={selectedProjectId}
          onChange={e => setSelectedProjectId(e.target.value)}
          className='w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2.5 text-sm text-gray-200 focus:outline-none focus:border-violet-500'
        >
          <option value=''>Select a project…</option>
          {projects.map(p => <option key={p.id} value={p.id.toString()}>{p.name}</option>)}
        </select>
      </div>

      {error && (
        <div className='bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-2.5 text-sm text-red-300 mb-3'>
          {error}
        </div>
      )}

      {/* Conversation */}
      <div className='flex-1 overflow-y-auto space-y-3 pb-2'>
        {messages.length === 0 && !thinking && (
          <div className='text-center text-gray-500 text-sm py-12'>
            {selectedProject
              ? <>Describe what you want to build or improve in <span className='text-violet-300'>{selectedProject.name}</span>.</>
              : 'Pick a project, then describe an idea. Ask “what should we improve?” to explore.'}
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
            <div className={
              'max-w-[85%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap leading-relaxed ' +
              (m.role === 'user'
                ? 'bg-violet-600 text-white rounded-br-sm'
                : 'bg-[#161927] border border-white/5 text-gray-200 rounded-bl-sm')
            }>
              {m.content}
            </div>
          </div>
        ))}

        {thinking && (
          <div className='flex justify-start'>
            <div className='bg-[#161927] border border-white/5 rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm text-gray-400 flex items-center gap-2'>
              <span className='w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin inline-block' />
              Reading the code…
            </div>
          </div>
        )}

        {/* Build proposal */}
        {proposal && !thinking && (
          <div className='bg-violet-600/10 border border-violet-500/40 rounded-xl p-4 space-y-3'>
            <div>
              <p className='text-xs uppercase tracking-wider text-violet-400 font-semibold mb-1'>Ready to build</p>
              <p className='text-sm text-white leading-snug'>{proposal.task}</p>
              {proposal.rationale && (
                <p className='text-xs text-gray-400 mt-1.5 leading-relaxed'>{proposal.rationale}</p>
              )}
            </div>
            <div className='flex gap-2'>
              <button
                onClick={build}
                disabled={dispatching}
                className='flex-1 py-2.5 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium disabled:opacity-40 transition-colors'
              >
                {dispatching ? 'Dispatching…' : '🚀 Build it'}
              </button>
              <button
                onClick={() => setProposal(null)}
                className='px-4 py-2.5 rounded-lg bg-white/5 active:bg-white/10 text-gray-300 text-sm'
              >
                Keep refining
              </button>
            </div>
          </div>
        )}

        {/* Dispatched builds */}
        {builds.map(b => (
          <a key={b.run_id} href={`/run/${b.run_id}`}
            className='block bg-green-600/10 border border-green-500/30 rounded-lg px-4 py-2.5 text-sm text-green-300 active:bg-green-600/20'>
            ✓ Building #{b.run_id} — tap to track
          </a>
        ))}

        <div ref={endRef} />
      </div>

      {/* Composer */}
      <div className='pt-2 sticky bottom-0 bg-[#0f1117]'>
        <div className='flex items-end gap-2'>
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={selectedProject ? 'Describe an idea…' : 'Pick a project first'}
            disabled={!selectedProject || thinking}
            rows={1}
            className='flex-1 bg-white/5 border border-white/10 rounded-lg px-3 py-2.5 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-violet-500 resize-none disabled:opacity-50'
          />
          <button
            onClick={send}
            disabled={!input.trim() || thinking || !selectedProject}
            className='px-4 py-2.5 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium disabled:opacity-40 transition-colors'
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
