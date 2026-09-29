import React, { useEffect, useRef, useState } from 'react';
import { getProjects } from '../api/client';
import type { Project } from '../api/client';

interface Msg {
  role: 'user' | 'manager';
  content: string;
}

interface Proposal {
  title: string;
  description: string;
  acceptance_criteria: string[];
  rationale: string;
}

interface FiledTicket {
  id: number;
  title: string;
  needsCriteria: boolean;
}

export default function Ideate() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [filing, setFiling] = useState(false);
  const [tickets, setTickets] = useState<FiledTicket[]>([]);
  const [error, setError] = useState('');
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getProjects().then(setProjects).catch(console.error);
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, thinking, proposal, tickets]);

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
      if (data.proposal?.title) setProposal(data.proposal);
    } catch (e) {
      setError(String(e));
    } finally {
      setThinking(false);
    }
  };

  // Accepting a proposal files a ticket in the backlog. It starts no build —
  // approving the ticket to `ready` is what releases it into the queue.
  const fileTicket = async () => {
    if (!proposal || !selectedProject?.project_path) return;
    setFiling(true);
    setError('');
    try {
      const res = await fetch('/api/manager/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: proposal.title,
          description: proposal.description ?? '',
          acceptance_criteria: proposal.acceptance_criteria ?? [],
          rationale: proposal.rationale ?? '',
          project_path: selectedProject.project_path,
          project_id: selectedProject.id,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Could not file the ticket');
      setTickets(prev => [...prev, {
        id: data.ticket?.id ?? data.ticket_id,
        title: proposal.title,
        needsCriteria: !!data.needs_acceptance_criteria,
      }]);
      setMessages(prev => [...prev, {
        role: 'manager',
        content: data.needs_acceptance_criteria
          ? `Filed ticket #${data.ticket_id} in the backlog. ${data.warning}`
          : `Filed ticket #${data.ticket_id} in the backlog. Approve it (move it to ready) and the queue will build it.`,
      }]);
      setProposal(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setFiling(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const rememberSession = () => {
    // Distill this session into the project's persistent vision memory (best-effort).
    if (messages.length && selectedProject?.project_path) {
      fetch('/api/manager/remember', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_path: selectedProject.project_path,
          history: messages.map(m => ({ role: m.role === 'user' ? 'user' : 'assistant', content: m.content })),
          project_name: selectedProject.name ?? '',
        }),
      }).catch(() => {});
    }
  };

  const reset = () => {
    rememberSession();
    setMessages([]); setProposal(null); setTickets([]); setError(''); setInput('');
  };

  return (
    <div className='max-w-2xl mx-auto flex flex-col h-full'>
      <div className='flex items-center justify-between mb-3'>
        <div>
          <h1 className='text-lg font-semibold text-white'>Ideate</h1>
          <p className='text-xs text-gray-500'>Talk through an idea — the manager grounds it in your code, then writes a ticket for it.</p>
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

        {/* Ticket proposal */}
        {proposal && !thinking && (
          <div className='bg-violet-600/10 border border-violet-500/40 rounded-xl p-4 space-y-3'>
            <div>
              <p className='text-xs uppercase tracking-wider text-violet-400 font-semibold mb-1'>Proposed ticket</p>
              <p className='text-sm text-white leading-snug font-medium'>{proposal.title}</p>
              {proposal.description && (
                <p className='text-sm text-gray-300 mt-1.5 leading-relaxed whitespace-pre-wrap'>{proposal.description}</p>
              )}
              {proposal.acceptance_criteria?.length ? (
                <div className='mt-2'>
                  <p className='text-xs uppercase tracking-wider text-gray-500 font-semibold mb-1'>Acceptance criteria</p>
                  <ul className='text-xs text-gray-300 space-y-1 list-disc list-inside leading-relaxed'>
                    {proposal.acceptance_criteria.map((c, i) => <li key={i}>{c}</li>)}
                  </ul>
                </div>
              ) : (
                <p className='text-xs text-amber-300 mt-2 leading-relaxed'>
                  No acceptance criteria — this ticket cannot be approved for build until it has
                  some. File it and add them, or ask the manager for them.
                </p>
              )}
              {proposal.rationale && (
                <p className='text-xs text-gray-400 mt-2 leading-relaxed'>{proposal.rationale}</p>
              )}
            </div>
            <div className='flex gap-2'>
              <button
                onClick={fileTicket}
                disabled={filing}
                className='flex-1 py-2.5 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium disabled:opacity-40 transition-colors'
              >
                {filing ? 'Filing…' : '📋 Add to backlog'}
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

        {/* Filed tickets */}
        {tickets.map(t => (
          <a key={t.id} href='/backlog'
            className={
              'block rounded-lg px-4 py-2.5 text-sm border ' +
              (t.needsCriteria
                ? 'bg-amber-600/10 border-amber-500/30 text-amber-300 active:bg-amber-600/20'
                : 'bg-green-600/10 border-green-500/30 text-green-300 active:bg-green-600/20')
            }>
            {t.needsCriteria
              ? `⚠ Ticket #${t.id} filed without acceptance criteria — add them before approving`
              : `✓ Ticket #${t.id} in the backlog — approve it to build`}
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
