import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { useInvalidate, useSettings } from '../api/hooks'
import {
  Button,
  Chip,
  cx,
  Drawer,
  EmptyState,
  Field,
  JsonBlock,
  MaskedValue,
  PageHeader,
  Select,
  StatusPill,
  TextArea,
  TextInput,
  timeAgo,
} from '../components/ui'

// spec §8.9 — the Ambient page: Routines (CRUD + fire-token lifecycle),
// Watches (standing intents + compiled-rule echo), Inbox (deliveries outbox
// with feedback capture), Ledger (fire/hold audit + intervention precision).

interface RoutineRow {
  id: string
  name: string
  description: string | null
  prompt: string
  source: string
  triggers: Record<string, unknown>[] | null
  allowlist: Record<string, unknown> | null
  autonomy: string
  budgets: Record<string, unknown> | null
  has_fire_token: boolean
  status: string
  status_reason: string | null
  consecutive_failures: number
  last_fired_at: string | null
  created_at: string | null
}

interface WatchRow {
  id: string
  text: string
  condition_type: string
  compiled: Record<string, unknown> | null
  semantic_predicate: string | null
  watermark: string | null
  cadence: {
    base_interval_s: number
    current_interval_s: number
    max_interval_s: number
    consecutive_quiet: number
    last_checked_at: string | null
  }
  expires_at: string | null
  delivery: string
  status: string
  created_at: string | null
}

export interface DeliveryRow {
  id: string
  run_id: string | null
  category: string
  tier: number
  urgency: number
  title: string
  body: string | null
  channel: string | null
  delivered_at: string | null
  superseded_by: string | null
  feedback: string | null
  seen_at?: string | null
  salience?: {
    verdict: string
    reason: string
    confidence: number
    applied: boolean
    mode?: string | null
    decision?: string | null // M43: applied | declined | undone
    decided_by?: string | null // 'user' or 'system' (auto mode)
  } | null
  reward: number | null
  created_at: string | null
}

interface LedgerRow {
  id: string
  kind: string
  source: string
  verdict: string | null
  verdict_reason: string | null
  decision: Record<string, unknown> | null
  depth: number
  correlation_id: string | null
  received_at: string | null
}

interface PrecisionRow {
  category: string
  precision: number | null
  judged: number
  series: number[]
  tier_override: number | null
  override_reason: string | null
  override_source: string | null
}

// ── §18.5 shared editors ──────────────────────────────────────────────

const FILTER_OPS = ['equals', 'contains', 'starts_with', 'one_of', 'regex'] as const

interface FilterDraft {
  field: string
  op: string
  value: string
}

export function FilterRowsEditor({
  rows,
  onChange,
}: {
  rows: FilterDraft[]
  onChange: (rows: FilterDraft[]) => void
}) {
  const update = (i: number, patch: Partial<FilterDraft>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5" data-testid="filter-rows">
      {rows.map((row, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <TextInput
            placeholder="field (e.g. sev or payload.repo)"
            value={row.field}
            onChange={(e) => update(i, { field: e.target.value })}
            className="flex-1"
          />
          <Select value={row.op} onChange={(e) => update(i, { op: e.target.value })} className="w-32">
            {FILTER_OPS.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </Select>
          <TextInput
            placeholder={row.op === 'one_of' ? 'a, b, c' : 'value'}
            value={row.value}
            onChange={(e) => update(i, { value: e.target.value })}
            className="flex-1"
          />
          <Button variant="ghost" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            ✕
          </Button>
        </div>
      ))}
      <Button variant="ghost" onClick={() => onChange([...rows, { field: '', op: 'equals', value: '' }])}>
        + filter
      </Button>
    </div>
  )
}

export function filterOut(row: FilterDraft): Record<string, unknown> {
  if (row.op === 'one_of') {
    return { field: row.field, op: row.op, value: '', values: row.value.split(',').map((v) => v.trim()).filter(Boolean) }
  }
  return { field: row.field, op: row.op, value: row.value }
}

interface TriggerDraft {
  type: 'interval' | 'cron' | 'once' | 'webhook'
  seconds: string
  cron: string
  at: string
  filters: FilterDraft[]
}

const EMPTY_TRIGGER: TriggerDraft = { type: 'interval', seconds: '3600', cron: '0 9 * * *', at: '', filters: [] }

export function triggerOut(t: TriggerDraft): Record<string, unknown> {
  if (t.type === 'interval') return { type: 'interval', seconds: Number(t.seconds) || 3600 }
  if (t.type === 'cron') return { type: 'cron', cron: t.cron }
  if (t.type === 'once') return { type: 'once', at: t.at }
  return { type: 'webhook', filters: t.filters.map(filterOut) }
}

export function TriggerBuilder({
  rows,
  onChange,
}: {
  rows: TriggerDraft[]
  onChange: (rows: TriggerDraft[]) => void
}) {
  const update = (i: number, patch: Partial<TriggerDraft>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-2" data-testid="trigger-builder">
      {rows.map((t, i) => (
        <div key={i} className="rounded-lg border border-slate-800 bg-void-950/40 p-2.5">
          <div className="flex items-center gap-1.5">
            <Select value={t.type} onChange={(e) => update(i, { type: e.target.value as TriggerDraft['type'] })} className="w-32">
              {(['interval', 'cron', 'once', 'webhook'] as const).map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
            {t.type === 'interval' && (
              <>
                <TextInput
                  value={t.seconds}
                  onChange={(e) => update(i, { seconds: e.target.value })}
                  className="w-28"
                />
                <span className="text-xs text-slate-500">seconds between fires (min 60)</span>
              </>
            )}
            {t.type === 'cron' && (
              <>
                <TextInput value={t.cron} onChange={(e) => update(i, { cron: e.target.value })} className="w-40" />
                <span className="text-xs text-slate-500">UTC cron expression</span>
              </>
            )}
            {t.type === 'once' && (
              <>
                <TextInput
                  placeholder="2026-09-01T09:00:00Z"
                  value={t.at}
                  onChange={(e) => update(i, { at: e.target.value })}
                  className="w-56"
                />
                <span className="text-xs text-slate-500">ISO timestamp, fires once</span>
              </>
            )}
            {t.type === 'webhook' && (
              <span className="text-xs text-slate-500">
                fires via the token endpoint — filters below gate each fire
              </span>
            )}
            <span className="ml-auto">
              <Button variant="ghost" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                ✕
              </Button>
            </span>
          </div>
          {t.type === 'webhook' && (
            <div className="mt-2">
              <FilterRowsEditor rows={t.filters} onChange={(filters) => update(i, { filters })} />
            </div>
          )}
        </div>
      ))}
      <Button variant="ghost" onClick={() => onChange([...rows, { ...EMPTY_TRIGGER }])}>
        + trigger
      </Button>
    </div>
  )
}

export function Sparkline({ series }: { series: number[] }) {
  // §18.5: the judged window as accept(1)/dismiss(0) ticks, chronological
  if (!series.length) return <span className="font-mono text-[10px] text-slate-600">no judged items</span>
  const w = 4
  return (
    <svg
      width={series.length * w}
      height={14}
      className="shrink-0"
      role="img"
      aria-label={`judged series: ${series.join('')}`}
    >
      {series.map((v, i) => (
        <rect
          key={i}
          x={i * w}
          y={v ? 1 : 8}
          width={w - 1}
          height={v ? 12 : 5}
          rx={1}
          className={v ? 'fill-emerald-400/80' : 'fill-rose-400/70'}
        />
      ))}
    </svg>
  )
}

const TIER_LABEL = ['interrupt', 'notify', 'digest', 'silent']
const TIER_TONE = [
  'text-rose-300 ring-rose-500/40',
  'text-amber-300 ring-amber-500/40',
  'text-sky-300 ring-sky-500/40',
  'text-slate-400 ring-slate-600',
]

function TierBadge({ tier }: { tier: number }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ring-1 ${TIER_TONE[tier] ?? TIER_TONE[3]}`}
    >
      {tier} · {TIER_LABEL[tier] ?? '?'}
    </span>
  )
}

function RoutinesTab() {
  const invalidate = useInvalidate()
  const { data } = useQuery({
    queryKey: ['routines'],
    queryFn: () => api.get<RoutineRow[]>('/routines'),
    refetchInterval: 10000,
  })
  const [selected, setSelected] = useState<RoutineRow | null>(null)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [autonomy, setAutonomy] = useState('propose')
  // §18.5: typed trigger builder is the default; raw JSON stays as the escape hatch
  const [triggerMode, setTriggerMode] = useState<'builder' | 'json'>('builder')
  const [triggerRows, setTriggerRows] = useState<TriggerDraft[]>([{ ...EMPTY_TRIGGER }])
  const [triggers, setTriggers] = useState('[{"type": "interval", "seconds": 3600}]')
  const [token, setToken] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const act = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      invalidate('routines')
    } catch (e) {
      setError(String(e))
    }
  }

  const create = () =>
    act(async () => {
      const parsed =
        triggerMode === 'builder'
          ? triggerRows.map(triggerOut)
          : triggers.trim()
            ? (JSON.parse(triggers) as unknown)
            : null
      await api.post('/routines', { name, prompt, autonomy, triggers: parsed })
      setCreating(false)
      setName('')
      setPrompt('')
    })

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <div className="text-xs text-slate-500">
          stored, trusted work definitions — fired by schedule, webhook token, or wakeup
        </div>
        <Button onClick={() => setCreating(true)}>New routine</Button>
      </div>
      {error && <div className="mb-2 text-xs text-rose-300">{error}</div>}
      <div className="overflow-hidden rounded-lg border border-slate-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-void-950/70 font-mono text-[10px] uppercase tracking-widest text-slate-500">
            <tr>
              <th className="px-3 py-2">name</th>
              <th className="px-3 py-2">status</th>
              <th className="px-3 py-2">triggers</th>
              <th className="px-3 py-2">autonomy</th>
              <th className="px-3 py-2">failures</th>
              <th className="px-3 py-2">last fired</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((r) => (
              <tr
                key={r.id}
                onClick={() => setSelected(r)}
                className="cursor-pointer border-t border-slate-800/60 hover:bg-slate-900/60"
              >
                <td className="px-3 py-2 font-medium text-slate-200">{r.name}</td>
                <td className="px-3 py-2">
                  <StatusPill status={r.status} title={r.status_reason ?? undefined} />
                </td>
                <td className="px-3 py-2 font-mono text-[11px] text-slate-400">
                  {(r.triggers ?? []).map((t) => String(t.type)).join(', ') || '—'}
                  {r.has_fire_token ? ' +token' : ''}
                </td>
                <td className="px-3 py-2 text-slate-400">{r.autonomy}</td>
                <td className="px-3 py-2 text-slate-400">{r.consecutive_failures}</td>
                <td className="px-3 py-2 text-slate-500">{timeAgo(r.last_fired_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(data ?? []).length === 0 && (
          <EmptyState>no routines yet — create one or POST /api/v1/routines</EmptyState>
        )}
      </div>

      <Drawer open={creating} onClose={() => setCreating(false)} title="New routine">
        <div className="space-y-3">
          <Field label="name">
            <TextInput value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="prompt (trusted instruction)">
            <TextArea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          </Field>
          <Field label="autonomy">
            <Select value={autonomy} onChange={(e) => setAutonomy(e.target.value)}>
              <option value="propose">propose (default)</option>
              <option value="act_reversible">act_reversible</option>
            </Select>
          </Field>
          <Field label="triggers">
            <div className="mb-1.5 flex gap-1.5">
              <Button
                variant={triggerMode === 'builder' ? 'primary' : 'ghost'}
                onClick={() => setTriggerMode('builder')}
              >
                builder
              </Button>
              <Button
                variant={triggerMode === 'json' ? 'primary' : 'ghost'}
                onClick={() => {
                  // hand the builder's current state to the escape hatch
                  setTriggers(JSON.stringify(triggerRows.map(triggerOut), null, 1))
                  setTriggerMode('json')
                }}
              >
                JSON
              </Button>
            </div>
            {triggerMode === 'builder' ? (
              <TriggerBuilder rows={triggerRows} onChange={setTriggerRows} />
            ) : (
              <TextArea rows={5} value={triggers} onChange={(e) => setTriggers(e.target.value)} />
            )}
          </Field>
          {error && <div className="text-xs text-rose-300">{error}</div>}
          <Button onClick={create} disabled={!name || !prompt}>
            Create
          </Button>
        </div>
      </Drawer>

      <Drawer open={!!selected} onClose={() => { setSelected(null); setToken(null) }} title={selected?.name ?? ''}>
        {selected && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill status={selected.status} />
              <Chip>{selected.source}</Chip>
              <Chip>{selected.autonomy}</Chip>
              {selected.consecutive_failures > 0 && (
                <Chip tone="direct">{selected.consecutive_failures} failures</Chip>
              )}
            </div>
            {selected.status_reason && (
              <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                {selected.status_reason}
              </div>
            )}
            <Field label="prompt">
              <div className="whitespace-pre-wrap rounded bg-void-950/60 p-3 text-xs text-slate-300">
                {selected.prompt}
              </div>
            </Field>
            <Field label="triggers">
              <JsonBlock value={selected.triggers} />
            </Field>
            {selected.allowlist && (
              <Field label="allowlist (narrowed projection)">
                <JsonBlock value={selected.allowlist} />
              </Field>
            )}
            <RoutineRunHistory routineId={selected.id} />
            {token && (
              <Field label="fire token (shown once — store it now)">
                <MaskedValue value={token} />
              </Field>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="ghost"
                onClick={() =>
                  act(async () => {
                    await api.patch(`/routines/${selected.id}`, {
                      status: selected.status === 'active' ? 'paused' : 'active',
                    })
                    setSelected(null)
                  })
                }
              >
                {selected.status === 'active' ? 'Pause' : 'Resume'}
              </Button>
              <Button
                variant="ghost"
                onClick={() =>
                  act(async () => {
                    const out = await api.post<{ fire_token: string }>(
                      `/routines/${selected.id}/token`,
                    )
                    setToken(out.fire_token)
                  })
                }
              >
                {selected.has_fire_token ? 'Rotate token' : 'Issue token'}
              </Button>
              {selected.has_fire_token && (
                <Button
                  variant="ghost"
                  onClick={() =>
                    act(async () => {
                      await api.delete(`/routines/${selected.id}/token`)
                      setToken(null)
                    })
                  }
                >
                  Revoke token
                </Button>
              )}
              <Button
                variant="danger"
                onClick={() =>
                  act(async () => {
                    await api.delete(`/routines/${selected.id}`)
                    setSelected(null)
                  })
                }
              >
                Delete
              </Button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  )
}

interface RunSummary {
  id: string
  status: string
  chat_message: string
  started_at: string | null
  total_input_tokens: number
  total_output_tokens: number
}

function RoutineRunHistory({ routineId }: { routineId: string }) {
  // §18.5: the drawer shows the routine's fires as ordinary runs
  const { data } = useQuery({
    queryKey: ['routine-runs', routineId],
    queryFn: () => api.get<RunSummary[]>(`/runs?routine_id=${routineId}`),
    refetchInterval: 10000,
  })
  return (
    <Field label={`run history (${data?.length ?? 0} fires)`}>
      <div className="max-h-56 space-y-1 overflow-y-auto" data-testid="routine-run-history">
        {(data ?? []).slice(0, 20).map((r) => (
          <div
            key={r.id}
            className="flex items-center gap-2 rounded border border-slate-800/70 bg-void-950/50 px-2.5 py-1.5"
          >
            <StatusPill status={r.status} />
            <span className="flex-1 truncate text-xs text-slate-300">
              {r.chat_message.replace(/^#[^\n]*\n?/, '').slice(0, 90) || r.chat_message.slice(0, 90)}
            </span>
            <span className="font-mono text-[10px] text-slate-500">
              {r.total_input_tokens}→{r.total_output_tokens}
            </span>
            <span className="font-mono text-[10px] text-slate-600">{timeAgo(r.started_at)}</span>
          </div>
        ))}
        {(data ?? []).length === 0 && (
          <div className="text-xs text-slate-600">no runs yet — this routine has never fired</div>
        )}
      </div>
    </Field>
  )
}

function WatchesTab() {
  const invalidate = useInvalidate()
  const { data } = useQuery({
    queryKey: ['watches'],
    queryFn: () => api.get<{ items: WatchRow[] }>('/watches'),
    refetchInterval: 10000,
  })
  const [selected, setSelected] = useState<WatchRow | null>(null)

  const setStatus = async (id: string, status: string) => {
    await api.patch(`/watches/${id}`, { status })
    invalidate('watches')
    setSelected(null)
  }

  return (
    <div>
      <div className="mb-3 text-xs text-slate-500">
        standing intents — compiled once from your words, evaluated by the scheduler, never
        remembered in model context
      </div>
      <WatchAuthoring onDone={() => invalidate('watches')} />
      <div className="space-y-2">
        {(data?.items ?? []).map((w) => (
          <div
            key={w.id}
            onClick={() => setSelected(w)}
            className="cursor-pointer rounded-lg border border-slate-800 bg-void-950/50 px-4 py-3 hover:bg-slate-900/60"
          >
            <div className="flex items-center gap-2">
              <StatusPill status={w.status} />
              <Chip>{w.condition_type}</Chip>
              <Chip>{w.delivery}</Chip>
              <span className="ml-auto font-mono text-[10px] text-slate-500">
                every {w.cadence.current_interval_s}s
                {w.cadence.consecutive_quiet > 0 ? ` · quiet ×${w.cadence.consecutive_quiet}` : ''}
              </span>
            </div>
            <div className="mt-1 text-sm text-slate-200">{w.text}</div>
          </div>
        ))}
      </div>
      {(data?.items ?? []).length === 0 && (
        <EmptyState>no watches — say “tell me when…” in chat</EmptyState>
      )}
      <Drawer open={!!selected} onClose={() => setSelected(null)} title="Standing watch">
        {selected && (
          <div className="space-y-4">
            <div className="text-sm text-slate-200">{selected.text}</div>
            <Field label="compiled rule (the echo you confirmed)">
              <JsonBlock value={selected.compiled} />
            </Field>
            {selected.semantic_predicate && (
              <Field label="semantic predicate (judged per candidate event)">
                <div className="rounded bg-void-950/60 p-3 text-xs text-slate-300">
                  {selected.semantic_predicate}
                </div>
              </Field>
            )}
            <div className="grid grid-cols-2 gap-2 font-mono text-[11px] text-slate-400">
              <div>watermark: {selected.watermark ?? '—'}</div>
              <div>base {selected.cadence.base_interval_s}s → now {selected.cadence.current_interval_s}s</div>
              <div>last check: {timeAgo(selected.cadence.last_checked_at)}</div>
              <div>expires: {selected.expires_at ? timeAgo(selected.expires_at) : 'never'}</div>
            </div>
            <div className="flex gap-2">
              {selected.status === 'proposed' && (
                <Button onClick={() => void setStatus(selected.id, 'active')}>Confirm</Button>
              )}
              {selected.status === 'active' && (
                <Button variant="ghost" onClick={() => void setStatus(selected.id, 'paused')}>
                  Pause
                </Button>
              )}
              {selected.status === 'paused' && (
                <Button variant="ghost" onClick={() => void setStatus(selected.id, 'active')}>
                  Resume
                </Button>
              )}
              <Button variant="danger" onClick={() => void setStatus(selected.id, 'retired')}>
                Retire
              </Button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  )
}

interface CompileResult {
  status: string
  intent_id: string
  interpretation: string
  compiled: Record<string, unknown>
}

function WatchAuthoring({ onDone }: { onDone: () => void }) {
  // §18.5: author from the page — NL through the SAME compiler as
  // ambient.watch (echo → confirm), or a typed event-filter watch directly
  const [mode, setMode] = useState<'describe' | 'typed'>('describe')
  const [text, setText] = useState('')
  const [filters, setFilters] = useState<FilterDraft[]>([{ field: '', op: 'equals', value: '' }])
  const [predicate, setPredicate] = useState('')
  const [proposal, setProposal] = useState<CompileResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const compile = () =>
    run(async () => {
      setProposal(await api.post<CompileResult>('/watches/compile', { text }))
      onDone()
    })
  const createTyped = () =>
    run(async () => {
      const out = await api.post<{ id: string; text: string; compiled: Record<string, unknown> }>(
        '/watches',
        {
          text,
          filters: filters.filter((f) => f.field).map(filterOut),
          semantic_predicate: predicate.trim() || null,
        },
      )
      setProposal({
        status: 'proposed',
        intent_id: out.id,
        interpretation: `Typed watch: ${out.text}`,
        compiled: out.compiled,
      })
      onDone()
    })
  const decide = (status: 'active' | 'retired') =>
    run(async () => {
      if (proposal) await api.patch(`/watches/${proposal.intent_id}`, { status })
      setProposal(null)
      setText('')
      onDone()
    })

  return (
    <div className="mb-4 rounded-lg border border-slate-800 bg-void-950/40 p-3" data-testid="watch-authoring">
      <div className="mb-2 flex items-center gap-1.5">
        <span className="mr-1 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          new watch
        </span>
        <Button variant={mode === 'describe' ? 'primary' : 'ghost'} onClick={() => setMode('describe')}>
          describe it
        </Button>
        <Button variant={mode === 'typed' ? 'primary' : 'ghost'} onClick={() => setMode('typed')}>
          typed filters
        </Button>
      </div>
      {mode === 'describe' ? (
        <div className="flex items-start gap-2">
          <TextArea
            rows={2}
            placeholder='"tell me when a high-severity alert appears in the ops feed…"'
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="flex-1"
          />
          <Button onClick={() => void compile()} disabled={busy || !text.trim()}>
            {busy ? 'Compiling…' : 'Compile'}
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <TextInput
            placeholder="what this watch is about (shown in the list)"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <FilterRowsEditor rows={filters} onChange={setFilters} />
          <TextInput
            placeholder="optional semantic predicate — a yes/no question judged per event"
            value={predicate}
            onChange={(e) => setPredicate(e.target.value)}
          />
          <Button onClick={() => void createTyped()} disabled={busy || !text.trim()}>
            Create proposed watch
          </Button>
        </div>
      )}
      {error && <div className="mt-2 text-xs text-rose-300">{error}</div>}
      {proposal && (
        <div className="mt-3 rounded-lg border border-accent-400/40 bg-accent-500/5 p-3" data-testid="watch-proposal">
          <div className="font-mono text-[10px] uppercase tracking-widest text-accent-300">
            proposed — confirm the interpretation
          </div>
          <div className="mt-1 text-sm text-slate-200">{proposal.interpretation}</div>
          <div className="mt-2">
            <JsonBlock value={proposal.compiled} />
          </div>
          <div className="mt-2 flex gap-2">
            <Button onClick={() => void decide('active')} disabled={busy}>
              Confirm
            </Button>
            <Button variant="ghost" onClick={() => void decide('retired')} disabled={busy}>
              Discard
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

function FeedbackButtons({ row, onDone }: { row: DeliveryRow; onDone: () => void }) {
  const give = async (feedback: string) => {
    await api.post(`/deliveries/${row.id}/feedback`, { feedback })
    onDone()
  }
  if (row.feedback) {
    return (
      <span className="font-mono text-[10px] text-slate-500">
        {row.feedback}
        {row.reward !== null ? ` · reward ${row.reward.toFixed(2)}` : ''}
      </span>
    )
  }
  return (
    <span className="flex gap-1">
      <Button variant="ghost" onClick={() => void give('accepted')}>✓</Button>
      <Button variant="ghost" onClick={() => void give('dismissed')}>✕</Button>
      <Button variant="ghost" onClick={() => void give('ignored')}>·</Button>
    </span>
  )
}

// M43 §8.9: the card leads with the CONSEQUENCE, never the mechanism —
// "Worth your attention" is what the verdict means to a person; "escalate"
// is what it means to the code. The mechanism stays available under
// "why this?", one click away, never omitted.
const CONSEQUENCE: Record<string, string> = {
  escalate: 'Worth your attention',
  retain: 'Worth remembering',
  drop: 'Looks like noise',
}
const PROPOSAL: Record<string, string> = {
  escalate: 'Lead the next digest with this.',
  retain: 'Save what this says to memory.',
  drop: 'Dismiss it.',
}
const APPLIED: Record<string, string> = {
  escalate: 'Leading the next digest.',
  retain: 'Saved to memory.',
  drop: 'Dismissed.',
}

export function SalienceBlock({ row, onDone }: { row: DeliveryRow; onDone: () => void }) {
  const s = row.salience
  const [why, setWhy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!s) return null
  const decision = s.decision ?? null
  const act = async (action: 'apply' | 'decline' | 'undo') => {
    setBusy(true)
    try {
      await api.post(`/deliveries/${row.id}/salience/${action}`)
      setError(null)
      onDone()
    } catch (e) {
      // a refusal is information, not a failure to hide — an escalation the
      // digest already carried genuinely cannot be taken back
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div
      data-testid="salience-verdict"
      className="mt-2 rounded-md border border-slate-800 bg-void-900/60 px-3 py-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-200">{CONSEQUENCE[s.verdict] ?? s.verdict}</span>
        <span className="text-[11px] text-slate-500">
          {decision === 'declined'
            ? 'Left as-is.'
            : decision === 'undone'
              ? 'Undone.'
              : s.applied
                ? APPLIED[s.verdict]
                : PROPOSAL[s.verdict]}
        </span>
        <span className="ml-auto flex items-center gap-1">
          {!decision && !s.applied && (
            <>
              <Button variant="secondary" disabled={busy} onClick={() => void act('apply')}>
                Do it
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => void act('decline')}>
                Leave it
              </Button>
            </>
          )}
          {s.applied && decision !== 'undone' && (
            <Button variant="ghost" disabled={busy} onClick={() => void act('undo')}>
              Undo
            </Button>
          )}
          <Button variant="ghost" onClick={() => setWhy((v) => !v)}>
            why this?
          </Button>
        </span>
      </div>
      {error && <div className="mt-1.5 text-[11px] text-rose-300">{error}</div>}
      {why && (
        <div
          data-testid="salience-why"
          className="mt-2 border-t border-slate-800 pt-2 font-mono text-[10px] text-slate-500"
        >
          A model judged this delivery after it went unseen — verdict{' '}
          <span className="text-accent-300">{s.verdict}</span>, confidence{' '}
          {s.confidence.toFixed(2)}, mode {s.mode ?? '—'}
          {decision ? ` · ${decision} by ${s.decided_by ?? 'user'}` : ''}
          <div className="mt-1 text-slate-400">{s.reason}</div>
        </div>
      )}
    </div>
  )
}

function DeliveryCard({ row, onDone }: { row: DeliveryRow; onDone: () => void }) {
  // M42 §18.4: opening an item stamps seen_at — attention becomes a fact.
  // Only delivered items can be "seen"; pending ones are not yet news.
  const unseen = Boolean(row.delivered_at) && !row.seen_at
  const markSeen = async () => {
    if (!unseen) return
    await api.post(`/deliveries/${row.id}/seen`).catch(() => {})
    onDone()
  }
  return (
    <div
      data-testid={unseen ? 'delivery-unseen' : 'delivery-seen'}
      onMouseEnter={() => void markSeen()}
      className={cx(
        'rounded-lg border px-4 py-3',
        unseen ? 'border-amber-500/40 bg-amber-500/[0.04]' : 'border-slate-800 bg-void-950/50',
      )}
    >
      <div className="flex items-center gap-2">
        <TierBadge tier={row.tier} />
        <Chip>{row.category}</Chip>
        <span className="font-mono text-[10px] text-slate-500">urgency {row.urgency}</span>
        <span className="ml-auto flex items-center gap-2">
          <span className="font-mono text-[10px] text-slate-600">
            {row.delivered_at ? `${row.channel} · ${timeAgo(row.delivered_at)}` : 'pending'}
          </span>
          <FeedbackButtons row={row} onDone={onDone} />
        </span>
      </div>
      <div className="mt-1 text-sm text-slate-200">{row.title}</div>
      {row.body && (
        <div className="mt-1 whitespace-pre-wrap text-xs text-slate-400">{row.body.slice(0, 600)}</div>
      )}
      <SalienceBlock row={row} onDone={onDone} />
    </div>
  )
}

function InboxTab() {
  const invalidate = useInvalidate()
  const { data: all } = useQuery({
    queryKey: ['deliveries'],
    queryFn: () => api.get<{ items: DeliveryRow[] }>('/deliveries?limit=100'),
    refetchInterval: 8000,
  })
  const { data: preview } = useQuery({
    queryKey: ['deliveries', 'digest-preview'],
    queryFn: () => api.get<{ items: DeliveryRow[] }>('/deliveries/digest-preview'),
    refetchInterval: 8000,
  })
  const refresh = () => invalidate('deliveries')
  const items = all?.items ?? []
  const delivered = items.filter((d) => d.delivered_at && d.channel !== 'silent')
  const silent = items.filter((d) => d.channel === 'silent')

  return (
    <div className="space-y-6">
      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          digest preview — what the next flush sends, urgency first
        </div>
        <div className="space-y-2">
          {(preview?.items ?? []).map((d) => (
            <DeliveryCard key={d.id} row={d} onDone={refresh} />
          ))}
        </div>
        {(preview?.items ?? []).length === 0 && <EmptyState>nothing queued for the digest</EmptyState>}
      </div>
      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          delivered — give feedback: it trains the tiering
        </div>
        <div className="space-y-2">
          {delivered.map((d) => (
            <DeliveryCard key={d.id} row={d} onDone={refresh} />
          ))}
        </div>
        {delivered.length === 0 && <EmptyState>nothing delivered yet</EmptyState>}
      </div>
      {silent.length > 0 && (
        <div>
          <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-slate-600">
            silent ledger — explicit decisions not to bother you
          </div>
          <div className="space-y-2 opacity-60">
            {silent.slice(0, 10).map((d) => (
              <DeliveryCard key={d.id} row={d} onDone={refresh} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

interface PolicyRow {
  id: string
  category: string
  tier_override: number | null
  reason: string
  source: string
  created_at: string | null
}

function ChainView({ correlationId }: { correlationId: string }) {
  // §18.5: the whole causal chain, cause → effect, indented by depth
  const { data } = useQuery({
    queryKey: ['ambient-chain', correlationId],
    queryFn: () => api.get<{ items: LedgerRow[] }>(`/ambient/ledger?correlation_id=${correlationId}`),
  })
  return (
    <div className="space-y-1 bg-void-950/70 px-4 py-3" data-testid="chain-view">
      <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
        correlation chain · {correlationId.slice(0, 8)}
      </div>
      {(data?.items ?? []).map((e, i) => (
        <div
          key={e.id}
          className="flex items-center gap-2 text-xs"
          style={{ paddingLeft: `${e.depth * 22}px` }}
        >
          <span className="font-mono text-slate-600">{i === 0 ? '●' : '↳'}</span>
          <span className="font-mono text-slate-300">{e.kind}</span>
          <Chip>{e.source}</Chip>
          <StatusPill status={e.verdict ?? 'pending'} />
          <span className="truncate text-slate-500">{e.verdict_reason ?? ''}</span>
          <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-600">
            {timeAgo(e.received_at)}
          </span>
        </div>
      ))}
    </div>
  )
}

function LedgerRowView({
  row,
  expanded,
  onToggle,
}: {
  row: LedgerRow
  expanded: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr
        onClick={onToggle}
        className="cursor-pointer border-t border-slate-800/60 hover:bg-slate-900/50"
        title="click to expand the correlation chain"
      >
        <td className="px-3 py-2 font-mono text-xs text-slate-300">{row.kind}</td>
        <td className="px-3 py-2 text-slate-400">{row.source}</td>
        <td className="px-3 py-2">
          <StatusPill status={row.verdict ?? 'pending'} />
        </td>
        <td className="max-w-md truncate px-3 py-2 text-xs text-slate-400" title={row.verdict_reason ?? ''}>
          {row.verdict_reason ?? '—'}
        </td>
        <td className="px-3 py-2 font-mono text-xs text-slate-500">
          {row.depth > 0 ? `↳${row.depth}` : '·'}
        </td>
        <td className="px-3 py-2 text-xs text-slate-500">{timeAgo(row.received_at)}</td>
      </tr>
      {expanded && row.correlation_id && (
        <tr className="border-t border-slate-800/40">
          <td colSpan={6} className="p-0">
            <ChainView correlationId={row.correlation_id} />
          </td>
        </tr>
      )}
    </>
  )
}

function LedgerTab() {
  const invalidate = useInvalidate()
  const [verdict, setVerdict] = useState('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const { data } = useQuery({
    queryKey: ['ambient-ledger', verdict],
    queryFn: () => api.get<{ items: LedgerRow[] }>(`/ambient/ledger?verdict=${verdict}`),
    refetchInterval: 10000,
  })
  const { data: precision } = useQuery({
    queryKey: ['ambient-precision'],
    queryFn: () => api.get<{ items: PrecisionRow[] }>('/ambient/precision'),
    refetchInterval: 15000,
  })
  const { data: policies } = useQuery({
    queryKey: ['ambient-policies'],
    queryFn: () => api.get<{ items: PolicyRow[] }>('/ambient/policies'),
    refetchInterval: 15000,
  })
  const proposals = (policies?.items ?? []).filter((p) => p.source === 'learner_proposal')

  const revert = async (category: string) => {
    await api.post('/ambient/policies/revert', { category })
    invalidate('ambient-precision', 'ambient-policies')
  }
  const approve = async (id: string) => {
    await api.post(`/ambient/policies/${id}/approve`)
    invalidate('ambient-precision', 'ambient-policies')
  }
  const reject = async (id: string) => {
    // M44 §17.7: captured, never applied — the proposal stops sitting pending
    await api.post(`/ambient/policies/${id}/reject`)
    invalidate('ambient-precision', 'ambient-policies')
  }

  return (
    <div className="space-y-6">
      {proposals.length > 0 && (
        <div>
          <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-amber-400">
            learning proposals — nothing applies until you approve
          </div>
          <div className="space-y-2">
            {proposals.map((p) => (
              <div
                key={p.id}
                className="flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2"
              >
                <Chip>{p.category}</Chip>
                <span className="flex-1 text-xs text-slate-300">{p.reason}</span>
                <Button onClick={() => void approve(p.id)}>Approve</Button>
                <Button variant="ghost" onClick={() => void reject(p.id)}>
                  Reject
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          intervention precision per category — low precision auto-downgrades one tier
        </div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {(precision?.items ?? []).map((p) => (
            <div
              key={p.category}
              className="flex items-center gap-2 rounded-lg border border-slate-800 bg-void-950/50 px-3 py-2"
            >
              <Chip>{p.category}</Chip>
              <span className="font-mono text-xs text-slate-300">
                {p.precision === null ? '—' : `${(p.precision * 100).toFixed(0)}%`}
              </span>
              <Sparkline series={p.series ?? []} />
              <span className="font-mono text-[10px] text-slate-600">{p.judged} judged</span>
              {p.tier_override !== null && (
                <>
                  <span
                    className="font-mono text-[10px] text-amber-300"
                    title={p.override_reason ?? undefined}
                  >
                    → tier {p.tier_override} ({p.override_source})
                  </span>
                  <Button variant="ghost" onClick={() => void revert(p.category)}>
                    revert
                  </Button>
                </>
              )}
            </div>
          ))}
        </div>
      </div>
      <div>
        <div className="mb-2 flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
            fire / hold audit
          </span>
          <Select value={verdict} onChange={(e) => setVerdict(e.target.value)} className="max-w-32">
            {['all', 'fired', 'held', 'dropped', 'expired', 'pending'].map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </Select>
        </div>
        <div className="overflow-hidden rounded-lg border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-void-950/70 font-mono text-[10px] uppercase tracking-widest text-slate-500">
              <tr>
                <th className="px-3 py-2">kind</th>
                <th className="px-3 py-2">source</th>
                <th className="px-3 py-2">verdict</th>
                <th className="px-3 py-2">reason</th>
                <th className="px-3 py-2">depth</th>
                <th className="px-3 py-2">when</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((e) => (
                <LedgerRowView
                  key={e.id}
                  row={e}
                  expanded={expanded === e.id}
                  onToggle={() => setExpanded(expanded === e.id ? null : e.id)}
                />
              ))}
            </tbody>
          </table>
          {(data?.items ?? []).length === 0 && <EmptyState>no events yet</EmptyState>}
        </div>
      </div>
    </div>
  )
}

export function AmbientPage() {
  const { data: settings } = useSettings()
  const [tab, setTab] = useState<'routines' | 'watches' | 'inbox' | 'ledger'>('inbox')
  const enabled = Boolean(settings?.ambient_enabled)

  return (
    <div className="p-6">
      <PageHeader
        title="Ambient"
        subtitle="proactive work while you're away — every fire and every silence is a ledgered decision"
      />
      {!enabled ? (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          ambient mode is off (`ambient_enabled` false) — nothing observes, fires, or delivers.
          Enable it in Settings → Ambient.
        </div>
      ) : (
        <>
          <div className="mb-4 flex items-center gap-2">
            {(
              [
                ['inbox', 'Inbox'],
                ['routines', 'Routines'],
                ['watches', 'Watches'],
                ['ledger', 'Ledger'],
              ] as const
            ).map(([key, label]) => (
              <Button
                key={key}
                variant={tab === key ? 'primary' : 'ghost'}
                onClick={() => setTab(key)}
              >
                {label}
              </Button>
            ))}
          </div>
          {tab === 'routines' && <RoutinesTab />}
          {tab === 'watches' && <WatchesTab />}
          {tab === 'inbox' && <InboxTab />}
          {tab === 'ledger' && <LedgerTab />}
        </>
      )}
    </div>
  )
}
