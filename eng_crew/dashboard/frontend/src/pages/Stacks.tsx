import { useEffect, useState } from 'react';
import { getStacks, setStack, setAgentOverrides } from '../api/client';
import type { StacksResponse } from '../api/client';

const AGENT_LABELS: Record<string, string> = {
  orchestrator:         'Orchestrator',
  architect:            'Architect',
  architect_phase2:     'Architect (Phase 2)',
  reviewer:             'Reviewer',
  executor:             'Executor',
  memory_writer:        'Memory Writer',
  coder:                'Generic Coder',
  frontend_coder:       'Frontend',
  backend_coder:        'Backend',
  database_coder:       'Database',
  ai_pipeline_coder:    'AI Pipeline',
  infrastructure_coder: 'Infrastructure',
};

const AGENT_GROUPS = [
  { label: 'Planning & Review', agents: ['orchestrator', 'architect', 'architect_phase2', 'reviewer', 'memory_writer'] },
  { label: 'Execution', agents: ['executor'] },
  { label: 'Coding', agents: ['coder', 'frontend_coder', 'backend_coder', 'database_coder', 'ai_pipeline_coder', 'infrastructure_coder'] },
];

const STACK_COLORS: Record<string, string> = {
  max:     'border-amber-500/40 bg-amber-500/5',
  quality: 'border-violet-500/40 bg-violet-500/5',
  fast:    'border-blue-500/40 bg-blue-500/5',
  gemini:  'border-cyan-500/40 bg-cyan-500/5',
  local:   'border-green-500/40 bg-green-500/5',
  cli:     'border-gray-500/40 bg-gray-500/5',
};

const PROVIDER_BADGE: Record<string, string> = {
  anthropic:  'bg-orange-500/15 text-orange-300',
  claude_cli: 'bg-orange-500/10 text-orange-400',
  gemini:     'bg-blue-500/15 text-blue-300',
  ollama:     'bg-green-500/15 text-green-300',
  openrouter: 'bg-purple-500/15 text-purple-300',
};

export default function Stacks() {
  const [data, setData] = useState<StacksResponse | null>(null);
  const [overrides, setOverrides] = useState<Record<string, { provider: string; model: string }>>({});
  const [saving, setSaving] = useState(false);
  const [switchingStack, setSwitchingStack] = useState('');
  const [saved, setSaved] = useState(false);

  const load = async () => {
    const d = await getStacks();
    setData(d);
    setOverrides(d.custom_overrides ?? {});
  };

  useEffect(() => { load().catch(console.error); }, []);

  const handleSelectStack = async (name: string) => {
    if (!data || name === data.active) return;
    setSwitchingStack(name);
    try {
      await setStack(name);
      await load();
    } catch (e) { console.error(e) }
    finally { setSwitchingStack('') }
  };

  const updateOverride = (agent: string, field: 'provider' | 'model', value: string) => {
    setOverrides(prev => {
      const base = data?.effective?.[agent] ?? { provider: '', model: '' };
      const current = prev[agent] ?? { provider: base.provider, model: base.model };
      const updated = { ...current, [field]: value };
      // If model list changes on provider switch, pick first available model
      if (field === 'provider' && data) {
        const models = data.available_models[value] ?? [];
        updated.model = models[0] ?? '';
      }
      return { ...prev, [agent]: updated };
    });
  };

  const handleSaveOverrides = async () => {
    setSaving(true);
    try {
      await setAgentOverrides(overrides);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      await load();
    } catch (e) { console.error(e) }
    finally { setSaving(false) }
  };

  const handleClearOverrides = async () => {
    setOverrides({});
    await setAgentOverrides({});
    await load();
  };

  const hasOverrides = Object.keys(overrides).length > 0;

  if (!data) return (
    <div className='flex items-center justify-center h-48 text-gray-400 text-sm'>Loading...</div>
  );

  return (
    <div className='max-w-2xl mx-auto space-y-6'>
      <h1 className='text-lg font-semibold text-white'>LLM Stack</h1>

      {/* Preset cards */}
      <div className='grid grid-cols-2 sm:grid-cols-3 gap-3'>
        {Object.entries(data.stacks).map(([name, cfg]) => {
          const isActive = data.active === name;
          const isSwitching = switchingStack === name;
          return (
            <button key={name} onClick={() => handleSelectStack(name)} disabled={!!switchingStack}
              className={'rounded-xl border p-3.5 text-left transition-all ' +
                (isActive
                  ? (STACK_COLORS[name] ?? 'border-violet-500/40 bg-violet-500/5') + ' ring-1 ring-inset ring-white/10'
                  : 'border-white/5 bg-[#161927] hover:border-white/15 active:border-white/20') +
                (isSwitching ? ' opacity-60' : '')}>
              <div className='flex items-center justify-between mb-1.5'>
                <span className='text-sm font-medium text-white capitalize'>{name}</span>
                {isActive && <span className='text-[10px] px-1.5 py-0.5 rounded-full bg-white/10 text-gray-300'>active</span>}
                {isSwitching && <span className='text-[10px] text-gray-400'>switching...</span>}
              </div>
              <p className='text-[11px] text-gray-400 leading-relaxed line-clamp-2'>
                {cfg.description.split('(')[0].trim()}
              </p>
            </button>
          );
        })}
      </div>

      {/* Per-agent customization */}
      <div className='space-y-4'>
        <div className='flex items-center justify-between'>
          <h2 className='text-sm font-medium text-gray-300'>Per-agent overrides</h2>
          {hasOverrides && (
            <button onClick={handleClearOverrides} className='text-xs text-red-400 hover:text-red-300 py-1'>
              Clear all overrides
            </button>
          )}
        </div>
        <p className='text-xs text-gray-500'>
          Customise individual agents. Overrides apply on top of the selected preset and persist across restarts.
        </p>

        {AGENT_GROUPS.map(group => (
          <div key={group.label}>
            <p className='text-[11px] uppercase tracking-wider text-gray-500 font-semibold mb-2'>{group.label}</p>
            <div className='space-y-1.5'>
              {group.agents.map(agent => {
                const effective = data.effective?.[agent];
                const override = overrides[agent];
                const currentProvider = override?.provider ?? effective?.provider ?? '';
                const currentModel = override?.model ?? effective?.model ?? '';
                const availableModels = data.available_models[currentProvider] ?? [];
                const isOverridden = !!override;

                return (
                  <div key={agent}
                    className={'flex flex-col sm:flex-row sm:items-center gap-2 p-3 rounded-xl border ' +
                      (isOverridden ? 'border-violet-500/30 bg-violet-500/5' : 'border-white/5 bg-[#161927]')}>
                    <div className='sm:w-36 flex items-center gap-2 flex-shrink-0'>
                      <span className='text-xs text-gray-200'>{AGENT_LABELS[agent] ?? agent}</span>
                      {isOverridden && <span className='text-[9px] px-1 rounded bg-violet-500/20 text-violet-300'>custom</span>}
                    </div>
                    <div className='flex gap-2 flex-1'>
                      {/* Provider */}
                      <div className="relative flex-1">
                        <select value={currentProvider}
                          onChange={e => updateOverride(agent, 'provider', e.target.value)}
                          className='w-full bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-violet-500 appearance-none'>
                          {Object.keys(data.available_models).map(p => (
                            <option key={p} value={p} className="bg-[#1a1d2d] text-white">{p}</option>
                          ))}
                        </select>
                        <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-2 text-gray-500">
                          <svg className="fill-current h-3 w-3" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="M9.293 12.95l.707.707L15.657 8l-1.414-1.414L10 10.828 5.757 6.586 4.343 8z"/></svg>
                        </div>
                      </div>

                      {/* Model */}
                      <div className="relative flex-1">
                        {availableModels.length > 0 ? (
                          <>
                            <select value={currentModel}
                              onChange={e => updateOverride(agent, 'model', e.target.value)}
                              className='w-full bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-violet-500 appearance-none'>
                              {availableModels.map(m => (
                                <option key={m} value={m} className="bg-[#1a1d2d] text-white">{m}</option>
                              ))}
                            </select>
                            <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-2 text-gray-500">
                              <svg className="fill-current h-3 w-3" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="M9.293 12.95l.707.707L15.657 8l-1.414-1.414L10 10.828 5.757 6.586 4.343 8z"/></svg>
                            </div>
                          </>
                        ) : (
                          <input value={currentModel}
                            onChange={e => updateOverride(agent, 'model', e.target.value)}
                            placeholder='model name'
                            className='w-full bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-xs text-white placeholder:text-gray-600 focus:outline-none focus:border-violet-500' />
                        )}
                      </div>
                      {/* Provider badge */}
                      <span className={'hidden sm:flex items-center text-[10px] px-2 rounded-md shrink-0 ' + (PROVIDER_BADGE[currentProvider] ?? 'bg-white/5 text-gray-400')}>
                        {currentProvider}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}

        <button onClick={handleSaveOverrides} disabled={saving}
          className='w-full py-3 rounded-lg bg-violet-600 active:bg-violet-700 text-white text-sm font-medium disabled:opacity-40 transition-colors'>
          {saving ? 'Saving...' : saved ? '✓ Saved' : 'Save overrides'}
        </button>
      </div>
    </div>
  );
}
