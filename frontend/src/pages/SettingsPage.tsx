/** Settings — the command center (spec §8.7): models + params, orchestrator
 * mode, limits, MCP controls, observability, HITL queue, data ops. Every
 * control maps to app_settings or an endpoint; nothing needs a restart. */
import { useState } from 'react'
import { api } from '../api/client'
import {
  useCacheStatus,
  useHitlPending,
  useInvalidate,
  usePatchSettings,
  useProviders,
  useRefreshCache,
  useServers,
  useSettings,
} from '../api/hooks'
import type { ModelParams } from '../api/types'
import { THEMES, applyTheme, currentTheme, type Theme } from '../theme'
import {
  Button,
  ErrorNote,
  Field,
  PageHeader,
  Select,
  StatusPill,
  TextInput,
  Toggle,
  cx,
  timeAgo,
} from '../components/ui'

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/30 p-4">
      <h2 className="mb-3 text-[11px] font-bold uppercase tracking-widest text-slate-500">
        {title}
      </h2>
      <div className="space-y-3">{children}</div>
    </section>
  )
}

function ModelSelect({
  label,
  refKey,
  paramsKey,
  allowInherit,
}: {
  label: string
  refKey: string
  paramsKey: string
  allowInherit?: boolean
}) {
  const { data: settings } = useSettings()
  const { data: providers = [] } = useProviders()
  const patch = usePatchSettings()
  if (!settings) return null
  const current = (settings[refKey] as string | null) ?? null
  const params = (settings[paramsKey] as ModelParams | null) ?? null
  const options = providers
    .filter((p) => p.configured)
    .flatMap((p) => p.models.map((m) => ({ ref: `${p.provider_id}:${m.id}`, supports: m })))
  const selected = options.find((o) => o.ref === current)
  const setParams = (patchObj: Partial<ModelParams>) => {
    const next: ModelParams = { ...(params ?? {}), ...patchObj }
    const clean: ModelParams = {}
    if (next.effort) clean.effort = next.effort
    if (next.temperature !== null && next.temperature !== undefined)
      clean.temperature = next.temperature
    if (next.max_output_tokens) clean.max_output_tokens = next.max_output_tokens
    patch.mutate({ [paramsKey]: Object.keys(clean).length ? clean : null })
  }
  return (
    <div className="grid grid-cols-4 items-end gap-2">
      <Field label={label}>
        <Select
          value={current ?? ''}
          onChange={(e) =>
            patch.mutate({
              [refKey]: e.target.value || null,
              ...(e.target.value ? {} : { [paramsKey]: null }),
            })
          }
        >
          {allowInherit && <option value="">(use default)</option>}
          {current && !options.some((o) => o.ref === current) && (
            <option value={current}>{current} (unconfigured)</option>
          )}
          {options.map((o) => (
            <option key={o.ref} value={o.ref}>
              {o.ref}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="effort">
        <Select
          disabled={!current || !selected?.supports.supports_effort}
          value={params?.effort ?? ''}
          onChange={(e) =>
            setParams({ effort: (e.target.value || undefined) as ModelParams['effort'] })
          }
        >
          <option value="">default</option>
          {['none', 'low', 'medium', 'high'].map((x) => (
            <option key={x}>{x}</option>
          ))}
        </Select>
      </Field>
      <Field label="temp">
        <TextInput
          type="number"
          step="0.1"
          disabled={!current || !selected?.supports.supports_temperature}
          defaultValue={params?.temperature ?? ''}
          onBlur={(e) =>
            setParams({ temperature: e.target.value === '' ? undefined : Number(e.target.value) })
          }
        />
      </Field>
      <Field label="max out">
        <TextInput
          type="number"
          disabled={!current || !selected?.supports.supports_max_output_tokens}
          defaultValue={params?.max_output_tokens ?? ''}
          onBlur={(e) =>
            setParams({
              max_output_tokens: e.target.value === '' ? undefined : Number(e.target.value),
            })
          }
        />
      </Field>
    </div>
  )
}

function CacheStatusPanel() {
  const { data: status } = useCacheStatus()
  const refresh = useRefreshCache()
  if (!status) return null
  return (
    <Field
      label="Cache status"
      hint="per-registry records · generation · loaded-at — refresh is an operator override, freshness is event-driven"
    >
      <div className="space-y-1.5">
        {Object.entries(status.registries).map(([name, entry]) => (
          <div key={name} className="flex items-center gap-3 font-mono text-[11px]">
            <span className="w-24 tracking-wider text-slate-400 uppercase">{name}</span>
            <span className="text-slate-500">
              {status.mode === 'bypass'
                ? `gen ${entry.generation} · bypass (direct db reads)`
                : entry.records != null
                  ? `${entry.records} records · gen ${entry.generation}` +
                    (entry.loaded_at ? ` · loaded ${timeAgo(entry.loaded_at)}` : '')
                  : 'not loaded'}
            </span>
          </div>
        ))}
        <div className="pt-1.5">
          <Button
            variant="secondary"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate('all')}
          >
            {refresh.isPending ? 'Refreshing…' : '⟳ Refresh all caches'}
          </Button>
        </div>
      </div>
    </Field>
  )
}

function IntSetting({
  label,
  k,
  hint,
  min = 1,
}: {
  label: string
  k: string
  hint?: string
  min?: number
}) {
  const { data: settings } = useSettings()
  const patch = usePatchSettings()
  if (!settings) return null
  return (
    <Field label={label} hint={hint}>
      <div className="space-y-1">
        <TextInput
          type="number"
          defaultValue={Number(settings[k])}
          className="max-w-28"
          onBlur={(e) => {
            const v = Number(e.target.value)
            // only nonsense is blocked here — spec-range checks stay server-side
            // so an out-of-range write surfaces its 422 inline (§14e-42)
            if (v >= min && v !== Number(settings[k])) patch.mutate({ [k]: v })
          }}
        />
        <ErrorNote error={patch.error} />
      </div>
    </Field>
  )
}

// ── M40 section helpers (exported for tests) ─────────────────────

/** Comma list → trimmed entries; server-side validation owns the format. */
export const csvOut = (text: string): string[] =>
  text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)

/** One mode's channel list edited → the full §18.4 routing map to PATCH
 * (empty list drops the mode: `{}` means in-app only). */
export const channelRoutingOut = (
  routing: Record<string, string[]>,
  mode: string,
  text: string,
): Record<string, string[]> => {
  const next = { ...routing }
  const chans = csvOut(text)
  if (chans.length) next[mode] = chans
  else delete next[mode]
  return next
}

function ListSetting({ label, k, hint }: { label: string; k: string; hint?: string }) {
  const { data: settings } = useSettings()
  const patch = usePatchSettings()
  if (!settings) return null
  return (
    <Field label={label} hint={hint}>
      <div className="space-y-1">
        <TextInput
          defaultValue={((settings[k] as string[]) ?? []).join(', ')}
          onBlur={(e) => patch.mutate({ [k]: csvOut(e.target.value) })}
        />
        <ErrorNote error={patch.error} />
      </div>
    </Field>
  )
}

function ChannelRouting() {
  const { data: settings } = useSettings()
  const patch = usePatchSettings()
  if (!settings) return null
  const routing = (settings.ambient_channels as Record<string, string[]>) ?? {}
  return (
    <Field
      label="Delivery channels (§18.4)"
      hint="per-mode routing — in_app always renders; add email / webhook (env-configured). Empty everywhere = in-app only"
    >
      <div className="grid grid-cols-3 gap-2">
        {(['interrupt', 'notify', 'digest'] as const).map((mode) => (
          <div key={mode}>
            <div className="mb-1 font-mono text-[9px] uppercase tracking-widest text-slate-600">
              {mode}
            </div>
            <TextInput
              placeholder="e.g. in_app, email"
              defaultValue={(routing[mode] ?? []).join(', ')}
              onBlur={(e) =>
                patch.mutate({ ambient_channels: channelRoutingOut(routing, mode, e.target.value) })
              }
            />
          </div>
        ))}
      </div>
      <ErrorNote error={patch.error} />
    </Field>
  )
}

function BoolSetting({ label, k, hint }: { label: string; k: string; hint?: string }) {
  const { data: settings } = useSettings()
  const patch = usePatchSettings()
  if (!settings) return null
  return (
    <Field label={label} hint={hint}>
      <Toggle checked={Boolean(settings[k])} onChange={(v) => patch.mutate({ [k]: v })} />
    </Field>
  )
}

export function SettingsPage() {
  const { data: settings } = useSettings()
  const { data: providers = [] } = useProviders()
  const { data: servers = [] } = useServers()
  const { data: pending = [] } = useHitlPending()
  const patch = usePatchSettings()
  const invalidate = useInvalidate()
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [theme, setTheme] = useState<Theme>(currentTheme())

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    setError(null)
    try {
      await fn()
      invalidate('mcp-servers', 'tools', 'runs', 'skills', 'sub-agents')
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  if (!settings) return <div className="p-6 text-sm text-slate-500">Loading…</div>

  return (
    <div className="space-y-4 p-6 pb-16">
      <PageHeader
        title="Settings"
        subtitle="Every runtime control, live — changes apply to the next run. API keys stay env-only, never here."
      />
      <ErrorNote error={patch.error ?? error} />

      <Section title="Models">
        <ModelSelect label="Default model" refKey="default_model" paramsKey="default_model_params" />
        <ModelSelect
          label="Planner model"
          refKey="planner_model"
          paramsKey="planner_model_params"
          allowInherit
        />
        <ModelSelect
          label="Aggregator model"
          refKey="aggregator_model"
          paramsKey="aggregator_model_params"
          allowInherit
        />
        <div className="mt-2 rounded-md border border-slate-800 p-3">
          <div className="mb-2 text-[10px] font-bold uppercase tracking-widest text-slate-600">
            Provider adapters (code-registered, read-only)
          </div>
          <div className="grid grid-cols-2 gap-2">
            {providers.map((p) => (
              <div
                key={p.provider_id}
                className={cx(
                  'rounded-md border px-3 py-2',
                  p.configured ? 'border-emerald-500/30' : 'border-slate-800 opacity-60',
                )}
              >
                <div className="flex items-center justify-between">
                  <code className="text-xs font-semibold text-slate-200">{p.provider_id}</code>
                  <span
                    className={cx(
                      'text-[10px] font-semibold uppercase',
                      p.configured ? 'text-emerald-400' : 'text-slate-500',
                    )}
                  >
                    {p.configured ? 'configured' : 'no api key'}
                  </span>
                </div>
                <div className="mt-1 text-[11px] text-slate-500">
                  {p.models.map((m) => m.id).join(' · ')}
                </div>
              </div>
            ))}
          </div>
        </div>
      </Section>

      <Section title="Orchestrator">
        <Field label="Mode" hint="switch per run — next chat message uses the new orchestrator">
          <div className="flex gap-2">
            {(['graph', 'agentic'] as const).map((m) => (
              <Button
                key={m}
                variant={settings.orchestrator_mode === m ? 'primary' : 'secondary'}
                onClick={() => patch.mutate({ orchestrator_mode: m })}
              >
                {m === 'graph' ? '🗺 graph (explicit planner)' : '🤖 agentic (todo-driven)'}
              </Button>
            ))}
          </div>
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <BoolSetting
            label="Full-catalog fallback"
            k="orchestrator_full_fallback_enabled"
            hint="when routing fails, the orchestrator handles it itself with ALL tools+skills"
          />
          <BoolSetting
            label="Dynamic worker fallback"
            k="dynamic_worker_fallback_enabled"
            hint="rung 4: build ephemeral workers on the fly"
          />
          <IntSetting label="Max parallel dispatch" k="max_parallel_dispatch" />
          <IntSetting label="Max plan steps" k="max_plan_steps" />
          <IntSetting
            label="Max tool iterations"
            k="max_tool_iterations"
            hint="per skill-node loop; exceeding fails the node (error-edge semantics)"
          />
          <IntSetting
            label="Direct-exposure cap warning"
            k="direct_exposure_cap_warning"
            hint="Tools/Skills pages warn above this"
          />
          <IntSetting
            label="Overlap-guard threshold (%)"
            k="overlap_threshold_percent"
            min={0}
            hint="saves at or above this overlap raise the confirm dialog — 100 effectively disables it, 0 flags every save (§4, M40)"
          />
          <IntSetting
            label="Agentic recursion limit"
            k="agentic_recursion_limit"
            hint="LangGraph recursion budget for the agentic loop (10–500); the model-call limit stays derived from max tool iterations"
          />
        </div>
      </Section>

      <Section title="Formatter (structured answers)">
        <BoolSetting
          label="Formatter"
          k="formatter_enabled"
          hint="transforms each answer into a structured A2UI view on its own model call — off = no call, raw answer renders directly, no structured artifact exists"
        />
        {Boolean(settings?.formatter_enabled) && (
          <>
            <ModelSelect
              label="Formatter model"
              refKey="formatter_model"
              paramsKey="formatter_model_params"
              allowInherit
            />
            <Field
              label="Presentation"
              hint="a2ui first = structured view primary, raw collapsed · raw first = raw primary, structured collapsed. Frozen per run — history always renders what happened at run time"
            >
              <div className="flex gap-2">
                {(['a2ui_first', 'raw_first'] as const).map((p) => (
                  <button
                    key={p}
                    onClick={() => patch.mutate({ formatter_presentation: p })}
                    className={`rounded-md border px-2.5 py-1 font-mono text-[11px] transition-colors ${
                      settings?.formatter_presentation === p
                        ? 'border-accent-500/50 bg-accent-500/10 text-accent-300'
                        : 'border-slate-700 text-slate-400 hover:border-slate-500'
                    }`}
                  >
                    {p === 'a2ui_first' ? 'A2UI first' : 'raw first'}
                  </button>
                ))}
              </div>
            </Field>
            <BoolSetting
              label="Charts in structured view"
              k="answer_ui_charts_enabled"
              hint="allow chart components (bar/line/pie) — data extracted from the answer, never invented"
            />
            <IntSetting
              label="Coverage flag threshold (%)"
              k="formatter_coverage_flag_threshold"
              hint="answers whose structured view retains fewer hard tokens (numbers/URLs/code) than this show an amber flag — visual only, never a render gate"
            />
          </>
        )}
      </Section>

      <Section title="Registry cache">
        <Field
          label="Mode"
          hint="bypass = direct db reads (rollback lever) · memory = in-process, event-invalidated · redis = shared backend (REDIS_URL env, pinged at save)"
        >
          <div className="flex gap-2">
            {(['bypass', 'memory', 'redis'] as const).map((m) => (
              <Button
                key={m}
                variant={settings.registry_cache_mode === m ? 'primary' : 'secondary'}
                onClick={() => patch.mutate({ registry_cache_mode: m })}
              >
                {m}
              </Button>
            ))}
          </div>
        </Field>
        <CacheStatusPanel />
      </Section>

      <Section title="Retrieval (progressive disclosure)">
        <div className="grid grid-cols-2 gap-4">
          <BoolSetting
            label="Top-K retrieval"
            k="retrieval_enabled"
            hint="rank orchestrator catalogs to the task's top-K above the threshold; skill loops stay id-pinned"
          />
          <IntSetting
            label="Threshold"
            k="retrieval_threshold"
            hint="registries at or below this size always inject in full"
          />
          <IntSetting label="Top K" k="retrieval_top_k" />
          <Field
            label="Embedding model"
            hint="provider:model (validated at save) — empty = lexical-only ranking"
          >
            <TextInput
              defaultValue={String(settings.embedding_model ?? '')}
              placeholder="e.g. openai:text-embedding-3-small"
              onBlur={(e) => patch.mutate({ embedding_model: e.target.value || null })}
            />
          </Field>
        </div>
      </Section>

      <Section title="Memory (§16 — the experiment layers)">
        <div className="grid grid-cols-2 gap-4">
          <BoolSetting
            label="Memory enabled"
            k="memory_enabled"
            hint="master switch — off is byte-identical to a memory-less build"
          />
          <BoolSetting
            label="Extraction (L2 writes)"
            k="memory_extraction_enabled"
            hint="post-run fact/preference extraction through the admission gate"
          />
          {/* M43 §8.7: the extraction role's model, reachable at last — the
              settings API has validated this key since the memory milestones */}
          <ModelSelect
            label="Extraction model"
            refKey="memory_extraction_model"
            paramsKey="memory_extraction_model_params"
            allowInherit
          />
          <BoolSetting
            label="Durable forgetting"
            k="memory_forget_enabled"
            hint="§16.1 — deletes become Forget (content-free tombstone, re-admission suppressed, undo via the Forgotten list) with Erase as the explicit no-trace verb. Off = deletes are physical and the system may re-learn a deleted fact"
          />
          {/* M48 §3.7.1 corollary: this configuration is legal but degraded,
              and used to be silently so — say it at the control */}
          {Boolean(settings.memory_forget_enabled) && !settings.embedding_model && (
            <p className="-mt-1 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
              Forgetting is on, but no embedding model is configured — suppression falls back to
              exact-text matching. A paraphrase of a forgotten fact will be re-learned. Set an
              embedding model above for semantic suppression.
            </p>
          )}
          <Field
            label="Forget similarity (0.5–1)"
            hint="semantic suppression threshold — paraphrases of a forgotten fact at or above this cosine similarity are refused; hash-only matching when no embedding model is set"
          >
            <div className="space-y-1">
              <TextInput
                defaultValue={String(settings.memory_forget_similarity ?? 0.85)}
                className="max-w-28"
                onBlur={(e) => patch.mutate({ memory_forget_similarity: Number(e.target.value) })}
              />
              <ErrorNote error={patch.error} />
            </div>
          </Field>
          <Field
            label="Admission floor (0–0.9)"
            hint="§16.2 — minimum extraction confidence to admit a machine write; the M47 learner moves this within [0.5, 0.9] when enabled"
          >
            <div className="space-y-1">
              <TextInput
                defaultValue={String(settings.memory_admission_min_confidence ?? 0.5)}
                className="max-w-28"
                onBlur={(e) =>
                  patch.mutate({ memory_admission_min_confidence: Number(e.target.value) })
                }
              />
              <ErrorNote error={patch.error} />
            </div>
          </Field>
          <Field
            label="Extraction learning"
            hint="M47 §17.7: the tombstone-informed tuner — routes chronically forgotten kinds through review and walks the admission floor; propose queues changes for approval, auto applies within clamps"
          >
            <Select
              value={String(settings.memory_extraction_learning ?? 'off')}
              onChange={(e) => patch.mutate({ memory_extraction_learning: e.target.value })}
              className="max-w-36"
            >
              {['off', 'propose', 'auto'].map((m) => (
                <option key={m}>{m}</option>
              ))}
            </Select>
          </Field>
          {Array.isArray(settings.memory_quarantine_kinds) &&
            settings.memory_quarantine_kinds.length > 0 && (
              <Field
                label="Kinds under review"
                hint="machine writes of these kinds land in the review queue instead of activating — clear a kind to trust it again"
              >
                <div className="flex flex-wrap gap-2">
                  {(settings.memory_quarantine_kinds as string[]).map((k) => (
                    <button
                      key={k}
                      className="rounded border border-slate-800/70 px-2 py-0.5 text-xs hover:opacity-70"
                      title="remove this kind from review routing"
                      onClick={() =>
                        patch.mutate({
                          memory_quarantine_kinds: (
                            settings.memory_quarantine_kinds as string[]
                          ).filter((x) => x !== k),
                        })
                      }
                    >
                      {k} ×
                    </button>
                  ))}
                </div>
              </Field>
            )}
          <BoolSetting
            label="Reflection (L4)"
            k="memory_reflection_enabled"
            hint="idle-time synthesis of higher-order memories (evidence-cited)"
          />
          {/* M48 §3.7.1: the consolidation jobs run on their own schedule,
              so each answers to its own switch — they differ in consequence */}
          <BoolSetting
            label="Decay sweep"
            k="memory_decay_enabled"
            hint="expires unpinned memories whose access-weighted importance falls below the floor; pinned rows are immune"
          />
          <BoolSetting
            label="Contradiction sweep"
            k="memory_contradiction_enabled"
            hint="quarantines the newer of two active memories that claim the same fact identity"
          />
          <BoolSetting
            label="Community rebuild"
            k="memory_communities_enabled"
            hint="§18.6 — groups related memories and summarizes each group with the extraction model; costs one model call per changed community"
          />
          <BoolSetting
            label="Digest compaction"
            k="memory_compaction_enabled"
            hint="folds old run digests into per-conversation period digests and HARD-DELETES the originals — the one consolidation job with an irreversible effect"
          />
          <IntSetting
            label="Compact digests after (days)"
            k="memory_digest_compact_days"
            hint="run digests older than this fold into a period digest"
          />
          <IntSetting
            label="Community budget (tokens)"
            k="memory_community_budget_tokens"
            hint="budget for the injected community-summary block; 0 turns communities off entirely — no injection and no rebuild"
          />
          <BoolSetting
            label="Procedural learning (L3)"
            k="procedural_learning_enabled"
            hint="routing stats + plan exemplars feeding the planner"
          />
          <IntSetting
            label="Injection budget (tokens)"
            k="memory_injection_budget_tokens"
            hint="per-surface cap for the remembered-context block"
          />
          <IntSetting label="Pinned budget (tokens)" k="memory_pinned_budget_tokens" />
          <IntSetting label="Recall top-K" k="memory_recall_top_k" />
          <Field label="Score floor (0–1)" hint="below it nothing injects — abstain over distract">
            <TextInput
              defaultValue={String(settings.memory_score_floor ?? 0.35)}
              onBlur={(e) => patch.mutate({ memory_score_floor: Number(e.target.value) })}
            />
          </Field>
          <Field label="Half-life (days)" hint="access-recency decay default">
            <TextInput
              defaultValue={String(settings.memory_half_life_days ?? 30)}
              onBlur={(e) => patch.mutate({ memory_half_life_days: Number(e.target.value) })}
            />
          </Field>
          <IntSetting label="Idle minutes" k="memory_idle_minutes" />
        </div>
      </Section>

      <Section title="MCP">
        <div className="flex items-end gap-4">
          <IntSetting label="Health-check interval (s)" k="mcp_health_interval_s" />
          <Button
            disabled={busy !== null}
            onClick={() =>
              act('reconnect-all', async () => {
                for (const s of servers) await api.post(`/mcp-servers/${s.id}/reconnect`)
              })
            }
          >
            {busy === 'reconnect-all' ? 'Reconnecting…' : 'Reconnect all'}
          </Button>
          <Button
            disabled={busy !== null}
            onClick={() =>
              act('refresh-all', async () => {
                for (const s of servers.filter((x) => x.status === 'active'))
                  await api.post(`/mcp-servers/${s.id}/refresh-tools`)
              })
            }
          >
            {busy === 'refresh-all' ? 'Refreshing…' : 'Refresh all tools'}
          </Button>
        </div>
      </Section>

      <Section title="Ambient (§17)">
        <BoolSetting
          label="Ambient mode"
          k="ambient_enabled"
          hint="master switch — the Ambient page appears in the nav while on; off is byte-identical"
        />
        {Boolean(settings.ambient_enabled) && (
          <>
            <div className="grid grid-cols-3 gap-4">
              <IntSetting
                label="Tick interval (s)"
                k="ambient_tick_interval_s"
                hint="scheduler cadence (min 15) — evaluators, drain heartbeat, and the parked-task poller all ride it"
              />
              <IntSetting
                label="Stall reaper window (s)"
                k="run_stall_after_s"
                hint="an ambient run silent longer than this is marked stalled (min 60, §17.4)"
              />
              <IntSetting label="Idle minutes" k="ambient_idle_minutes" />
              <IntSetting label="Max routines" k="ambient_max_routines" />
              <IntSetting label="Runs per day" k="ambient_runs_per_day" />
              <IntSetting label="Routine events / hour" k="ambient_routine_events_per_hour" />
              <IntSetting
                label="Wakeups / routine / day"
                k="ambient_wakeups_per_routine_per_day"
              />
              <IntSetting label="HITL timeout (h)" k="ambient_hitl_timeout_h" />
              <IntSetting
                label="Notification budget / day"
                k="ambient_notification_budget_per_day"
              />
              <IntSetting label="Escalation budget / day" k="ambient_escalation_budget_per_day" />
              <IntSetting
                label="Interrupt threshold"
                k="ambient_interrupt_threshold"
                hint="urgency at or above this may break quiet hours"
              />
              {/* M48 §3.7.1: the only feature that starts a conversation on
                  its own, so silence is a setting rather than only an outcome */}
              <BoolSetting
                label="Anticipation briefings"
                k="ambient_anticipation_enabled"
                hint="composes a short briefing of likely next asks when you have been idle. The only feature that contacts you without being asked — off means it never runs, regardless of how useful it has been"
              />
              <Field
                label="Learning mode"
                hint="§17.7 — auto applies policy tweaks itself, propose queues them for approval"
              >
                <Select
                  value={String(settings.ambient_learning_mode)}
                  onChange={(e) => patch.mutate({ ambient_learning_mode: e.target.value })}
                  className="max-w-36"
                >
                  {['off', 'auto', 'propose'].map((m) => (
                    <option key={m}>{m}</option>
                  ))}
                </Select>
              </Field>
              <BoolSetting
                label="Precision auto-downgrade"
                k="ambient_precision_rule_enabled"
                hint="§17.3 static rule (learning off): your ✕ clicks train the tiering — a chronically dismissed category drops one tier. Off = feedback is still captured, but never re-tiers a category"
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <ListSetting
                label="Digest times"
                k="ambient_digest_times"
                hint="comma-separated HH:MM — when tier-2 digests flush"
              />
              <ListSetting
                label="Quiet hours"
                k="ambient_quiet_hours"
                hint="HH:MM start, HH:MM end — non-urgent delivery holds in between"
              />
            </div>
            <ChannelRouting />
            <Field
              label="Salience (§17.5)"
              hint="re-judges what an unseen alert actually SAID: lead the next digest, remember the fact, or drop it on the record. Never re-interrupts and never overrides quiet hours"
            >
              <div className="flex items-end gap-3">
                <Select
                  value={String(settings.ambient_salience_mode)}
                  onChange={(e) => patch.mutate({ ambient_salience_mode: e.target.value })}
                  className="max-w-36"
                >
                  {['off', 'propose', 'auto'].map((m) => (
                    <option key={m}>{m}</option>
                  ))}
                </Select>
                <div className="w-40">
                  <IntSetting
                    label="Min urgency"
                    k="ambient_salience_min_urgency"
                    hint="prefilter floor (1-5) before a model is called; a recurring alert clears it anyway"
                  />
                </div>
              </div>
              <Field
                label="Salience learning"
                hint="FLE: the tuner over your Do it / Leave it decisions — proposes category mutes and floor moves; propose queues them for approval, auto applies within clamps"
              >
                <Select
                  value={String(settings.ambient_salience_learning ?? 'off')}
                  onChange={(e) => patch.mutate({ ambient_salience_learning: e.target.value })}
                  className="max-w-36"
                >
                  {['off', 'propose', 'auto'].map((m) => (
                    <option key={m}>{m}</option>
                  ))}
                </Select>
              </Field>
              {/* M43 §8.7: every role model has a picker in the section that
                  owns it — this one was validated by the API since M42 with
                  no UI to reach it */}
              <div className="mt-3">
                <ModelSelect
                  label="Salience judge model"
                  refKey="ambient_salience_model"
                  paramsKey="ambient_salience_model_params"
                  allowInherit
                />
              </div>
            </Field>
            <Field
              label="Pursuit (§17.5)"
              hint="when the external channels above actually fire — away = only when the in-app toast reached nobody; always = whenever routed (pre-M41); off = in-app only. Never overrides quiet hours, tiers, or the budget"
            >
              <Select
                value={String(settings.ambient_pursuit)}
                onChange={(e) => patch.mutate({ ambient_pursuit: e.target.value })}
                className="max-w-36"
              >
                {['off', 'away', 'always'].map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </Select>
            </Field>
          </>
        )}
      </Section>

      <Section title="A2A — remote agents (§19)">
        <BoolSetting
          label="A2A"
          k="a2a_enabled"
          hint="master switch — the Remote Agents page appears in the nav while on; off is byte-identical"
        />
        {Boolean(settings.a2a_enabled) && (
          <div className="grid grid-cols-3 gap-4">
            <IntSetting label="Card refresh interval (s)" k="a2a_card_refresh_interval_s" />
            <IntSetting
              label="Task timeout (s)"
              k="a2a_task_timeout_s"
              hint="in-run wait budget before park-or-error (§19.5)"
            />
            <IntSetting
              label="Poll interval (s)"
              k="a2a_poll_interval_s"
              hint="parked-task recheck cadence — tick-bounded, effective max(tick, interval)"
            />
            <IntSetting
              label="Max parked"
              k="a2a_max_parked"
              min={0}
              hint="0 disables parking — budget expiry becomes a plain tool error"
            />
            <IntSetting
              label="HTTP timeout (s)"
              k="a2a_http_timeout_s"
              hint="shared A2A client — applies on the manager's next client build"
            />
            <IntSetting
              label="Fence cap (chars)"
              k="a2a_fence_max_chars"
              hint="max chars of fenced remote output reaching model context (min 500, §19.5)"
            />
          </div>
        )}
      </Section>

      <Section title="API guardrails">
        <div className="grid grid-cols-2 gap-4">
          <IntSetting
            label="Rate-limit burst"
            k="rate_limit_burst"
            hint="§18.8 token-bucket size per user — enforced while auth is on"
          />
          <IntSetting
            label="Rate-limit refill (/s)"
            k="rate_limit_per_s"
            hint="tokens restored per second per user"
          />
        </div>
        <BoolSetting
          label="Evals surface"
          k="evals_enabled"
          hint="§15 — dataset upload and graded batch runs on skill and sub-agent pages. Nothing runs on its own; off removes the routes entirely, leaving datasets and past results intact for when it is turned back on"
        />
      </Section>

      <Section title="Observability">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Log level">
            <Select
              value={String(settings.log_level)}
              onChange={(e) => patch.mutate({ log_level: e.target.value })}
              className="max-w-36"
            >
              {['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((l) => (
                <option key={l}>{l}</option>
              ))}
            </Select>
          </Field>
          <BoolSetting
            label="LangSmith tracing"
            k="langsmith_enabled"
            hint="per-run tracer from these settings; LANGSMITH_API_KEY stays env-only"
          />
          <Field label="LangSmith endpoint" hint="empty = SaaS; point at a local instance to self-host">
            <TextInput
              defaultValue={String(settings.langsmith_endpoint)}
              onBlur={(e) => patch.mutate({ langsmith_endpoint: e.target.value })}
            />
          </Field>
          <Field label="LangSmith project">
            <TextInput
              defaultValue={String(settings.langsmith_project)}
              onBlur={(e) => patch.mutate({ langsmith_project: e.target.value })}
            />
          </Field>
          <Field label="OTLP endpoint" hint="runtime override of the env bootstrap default">
            <TextInput
              defaultValue={String(settings.otlp_endpoint)}
              onBlur={(e) => patch.mutate({ otlp_endpoint: e.target.value })}
            />
          </Field>
        </div>
      </Section>

      <Section title="HITL queue">
        {pending.length === 0 ? (
          <p className="text-xs text-slate-500">No runs waiting for approval.</p>
        ) : (
          <div className="space-y-2">
            {pending.map((p) => (
              <div
                key={p.run_id}
                className="flex items-center gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2"
              >
                <StatusPill status="paused_hitl" />
                <span className="flex-1 truncate text-xs text-slate-300">{p.chat_message}</span>
                <span className="text-[10px] text-slate-500">{timeAgo(p.started_at)}</span>
                <Button
                  variant="primary"
                  onClick={() =>
                    act('hitl', () =>
                      api.post(`/runs/${p.run_id}/hitl`, { decision: 'approve', note: '' }),
                    )
                  }
                >
                  Approve
                </Button>
                <Button
                  variant="danger"
                  onClick={() =>
                    act('hitl', () =>
                      api.post(`/runs/${p.run_id}/hitl`, { decision: 'deny', note: '' }),
                    )
                  }
                >
                  Deny
                </Button>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title="Appearance">
        <Field label="Theme" hint="stored in this browser — applies instantly, no backend setting">
          <div className="flex flex-wrap gap-2">
            {THEMES.map((t) => (
              <Button
                key={t}
                variant={theme === t ? 'primary' : 'secondary'}
                onClick={() => {
                  applyTheme(t)
                  setTheme(t)
                }}
              >
                <span
                  className="mr-1.5 inline-block size-2 rounded-full"
                  style={{
                    background:
                      t === 'default'
                        ? '#14b8a6'
                        : t === 'anthropic'
                          ? '#cc785c'
                          : t === 'openai'
                            ? '#10a37f'
                            : '#4285f4',
                  }}
                />
                {t}
              </Button>
            ))}
          </div>
        </Field>
      </Section>

      <Section title="Data">
        <div className="flex gap-2">
          <Button
            disabled={busy !== null}
            onClick={() => act('seed', () => api.post('/seed/reload'))}
          >
            {busy === 'seed' ? 'Reloading…' : 'Reload seed (idempotent)'}
          </Button>
          <Button
            variant="danger"
            disabled={busy !== null}
            onClick={() => {
              if (window.confirm('Purge ALL run history? This cannot be undone.'))
                void act('purge', () => api.delete('/runs'))
            }}
          >
            {busy === 'purge' ? 'Purging…' : 'Purge run history'}
          </Button>
        </div>
      </Section>
    </div>
  )
}
