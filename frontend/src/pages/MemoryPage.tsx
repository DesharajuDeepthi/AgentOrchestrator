import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useInvalidate, useSettings } from '../api/hooks'
import { Button, Chip, Drawer, Field, PageHeader, Select, StatusPill, TextInput } from '../components/ui'

// spec §16.6 — the Memory page: store browser, quarantine review queue,
// layer status. Every mutating control here is a USER action (edit-as-
// supersede, pin, review decisions, hard delete).

interface MemoryRow {
  id: string
  scope: string
  conversation_id: string | null
  kind: string
  text: string
  entity_key: string | null
  importance: number
  confidence: number
  source: string
  status: string
  valid_from: string
  valid_to: string | null
  recorded_at: string
  superseded_at: string | null
  supersedes: string | null
  superseded_by: string | null
  run_id: string | null
  last_accessed_at: string
  access_count: number
  pinned: boolean
  review_note: string | null
}

export interface TombstoneRow {
  id: string
  scope: string
  kind: string
  source: string
  project_key: string | null
  importance: number
  confidence: number
  age_days: number
  pinned: boolean
  forgotten_at: string | null
  suppressed_count: number
  last_suppressed_at: string | null
}

interface MemoryStatus {
  counts: Record<string, number>
  by_kind: Record<string, number>
  quarantined: number
  pinned: number
  embeddings: number
}

const KINDS = ['fact', 'preference', 'entity', 'relation', 'instruction']
const STATUSES = ['active', 'quarantined', 'superseded', 'expired', 'rejected']

function useMemories(filters: Record<string, string>) {
  const params = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v !== ''),
  ).toString()
  return useQuery({
    queryKey: ['memories', params],
    queryFn: () => api.get<MemoryRow[]>(`/memories${params ? `?${params}` : ''}`),
    refetchInterval: 15000,
  })
}

function useMemoryStatus() {
  return useQuery({
    queryKey: ['memories', 'status'],
    queryFn: () => api.get<MemoryStatus>('/memories/status'),
    refetchInterval: 15000,
  })
}

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-void-950/60 px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">{label}</div>
      <div className="mt-1 font-display text-xl font-bold text-slate-100">{value}</div>
    </div>
  )
}

function KindBadge({ kind }: { kind: string }) {
  const tones: Record<string, string> = {
    preference: 'text-sky-300 ring-sky-500/40',
    instruction: 'text-amber-300 ring-amber-500/40',
    entity: 'text-violet-300 ring-violet-500/40',
    relation: 'text-emerald-300 ring-emerald-500/40',
    fact: 'text-slate-300 ring-slate-600',
  }
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ring-1 ${tones[kind] ?? tones.fact}`}
    >
      {kind}
    </span>
  )
}

function MemoryDetail({
  row,
  onDone,
  forgetEnabled,
}: {
  row: MemoryRow
  onDone: () => void
  forgetEnabled: boolean
}) {
  const invalidate = useInvalidate()
  const [text, setText] = useState(row.text)
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)

  const act = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      invalidate('memories')
      onDone()
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <KindBadge kind={row.kind} />
        <StatusPill status={row.status} />
        <Chip>{row.source}</Chip>
        {row.pinned && <Chip tone="direct">pinned</Chip>}
        <span className="font-mono text-[10px] text-slate-500">imp {row.importance}/10</span>
        <span className="font-mono text-[10px] text-slate-500">
          conf {(row.confidence * 100).toFixed(0)}%
        </span>
        <span className="font-mono text-[10px] text-slate-500">accessed {row.access_count}×</span>
      </div>

      <Field label="Memory text" hint="saving a change supersedes this row — history is never rewritten">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-slate-700 bg-void-950/60 px-3 py-2 text-sm text-slate-200 focus:border-accent-400 focus:outline-none"
        />
      </Field>

      <div className="grid grid-cols-2 gap-3 font-mono text-[11px] text-slate-400">
        <div>
          <div className="text-slate-600">valid (event time)</div>
          {new Date(row.valid_from).toLocaleString()} →{' '}
          {row.valid_to ? new Date(row.valid_to).toLocaleString() : 'now'}
        </div>
        <div>
          <div className="text-slate-600">recorded (ingestion time)</div>
          {new Date(row.recorded_at).toLocaleString()}
          {row.superseded_at ? ` → superseded ${new Date(row.superseded_at).toLocaleString()}` : ''}
        </div>
        {row.entity_key && (
          <div>
            <div className="text-slate-600">entity key</div>
            {row.entity_key}
          </div>
        )}
        {row.run_id && (
          <div>
            <div className="text-slate-600">provenance</div>
            <a href={`#/runs?run=${row.run_id}`} className="text-accent-300 hover:underline">
              run {row.run_id.slice(0, 8)}
            </a>
          </div>
        )}
        {row.supersedes && (
          <div>
            <div className="text-slate-600">supersedes</div>
            {row.supersedes.slice(0, 8)}
          </div>
        )}
        {row.superseded_by && (
          <div>
            <div className="text-slate-600">superseded by</div>
            {row.superseded_by.slice(0, 8)}
          </div>
        )}
        {row.review_note && (
          <div className="col-span-2">
            <div className="text-slate-600">review note</div>
            {row.review_note}
          </div>
        )}
      </div>

      {row.status === 'quarantined' && (
        <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-3">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-amber-300">
            review required — this memory does not apply until approved
          </div>
          <TextInput
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="optional review note…"
          />
          <div className="mt-2 flex gap-2">
            <Button
              variant="primary"
              onClick={() =>
                void act(() =>
                  api.patch(`/memories/${row.id}`, { review: 'approve', review_note: note || null }),
                )
              }
            >
              ✓ Approve
            </Button>
            <Button
              variant="danger"
              onClick={() =>
                void act(() =>
                  api.patch(`/memories/${row.id}`, { review: 'reject', review_note: note || null }),
                )
              }
            >
              ✕ Reject
            </Button>
          </div>
        </div>
      )}

      {error && <div className="text-xs text-rose-400">{error}</div>}

      <div className="flex flex-wrap gap-2">
        {text !== row.text && (
          <Button variant="primary" onClick={() => void act(() => api.patch(`/memories/${row.id}`, { text }))}>
            Save as new version
          </Button>
        )}
        <Button
          onClick={() => void act(() => api.patch(`/memories/${row.id}`, { pinned: !row.pinned }))}
        >
          {row.pinned ? 'Unpin' : 'Pin (always injected)'}
        </Button>
        {forgetEnabled && (
          <Button
            variant="danger"
            onClick={() => {
              if (
                window.confirm(
                  'Forget this memory? It is removed and will NOT be re-learned — a content-free tombstone suppresses re-admission (undo via the Forgotten list).',
                )
              ) {
                void act(() => api.delete(`/memories/${row.id}?mode=forget`))
              }
            }}
          >
            Forget
          </Button>
        )}
        <Button
          variant="danger"
          onClick={() => {
            if (
              window.confirm(
                forgetEnabled
                  ? 'Erase completely? No trace remains — and the system MAY re-learn this fact later.'
                  : 'Hard-delete this memory? This is the only destructive path.',
              )
            ) {
              void act(() => api.delete(`/memories/${row.id}`))
            }
          }}
        >
          {forgetEnabled ? 'Erase completely' : 'Delete permanently'}
        </Button>
      </div>
    </div>
  )
}

// M44 §8.8: tombstones — metadata only, there is no text to show. The
// escape hatch (Unforget) sits beside every entry so suppression is never
// invisible or irreversible.
export function ForgottenSection() {
  const invalidate = useInvalidate()
  const { data } = useQuery({
    queryKey: ['memories', 'tombstones'],
    queryFn: () => api.get<{ items: TombstoneRow[] }>('/memories/tombstones'),
    refetchInterval: 15000,
  })
  const rows = data?.items ?? []
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800" data-testid="forgotten-section">
      <table className="w-full text-left text-sm">
        <thead className="bg-void-950/80 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          <tr>
            <th className="px-3 py-2">Kind</th>
            <th className="px-3 py-2">Scope</th>
            <th className="px-3 py-2">Source</th>
            <th className="px-3 py-2">Forgotten</th>
            <th className="px-3 py-2">Tried to return</th>
            <th className="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.id} className="border-t border-slate-800/60">
              <td className="px-3 py-2"><KindBadge kind={t.kind} /></td>
              <td className="px-3 py-2 font-mono text-[11px] text-slate-400">
                {t.scope}
                {t.project_key ? ` · ${t.project_key}` : ''}
              </td>
              <td className="px-3 py-2 font-mono text-[11px] text-slate-400">{t.source}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-slate-500">
                {t.forgotten_at ? new Date(t.forgotten_at).toLocaleString() : '—'}
              </td>
              <td className="px-3 py-2 font-mono text-[11px] text-slate-400">
                {t.suppressed_count > 0 ? `${t.suppressed_count}× suppressed` : 'never'}
              </td>
              <td className="px-3 py-2 text-right">
                <Button
                  variant="ghost"
                  onClick={() =>
                    void api.delete(`/memories/tombstones/${t.id}`).then(() => invalidate('memories'))
                  }
                >
                  Unforget
                </Button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="px-3 py-8 text-center text-sm text-slate-500">
                nothing forgotten — deletes leave a trace here only when Forget is used
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

export function MemoryPage() {
  const { data: settings } = useSettings()
  const forgetEnabled = Boolean(settings?.memory_forget_enabled)
  const [tab, setTab] = useState<'store' | 'review' | 'forgotten'>('store')
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<MemoryRow | null>(null)
  const [newText, setNewText] = useState('')
  const [newKind, setNewKind] = useState('fact')
  const invalidate = useInvalidate()

  const storeFilters = useMemo<Record<string, string>>(
    () => (tab === 'review' ? { status: 'quarantined', kind: '', q: '' } : { kind, status, q }),
    [tab, kind, status, q],
  )
  const { data: rows = [] } = useMemories(storeFilters)
  const { data: stat } = useMemoryStatus()

  const memoryEnabled = Boolean(settings?.memory_enabled)

  return (
    <div className="p-6">
      <PageHeader
        title="Memory"
        subtitle="the durable store — every row visible, editable, pinnable, deletable; machine writes carry provenance"
      />

      {!memoryEnabled && (
        <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          memory is disabled (`memory_enabled` off) — the store is inert and nothing injects.
          Enable it in Settings → Memory.
        </div>
      )}

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatCard label="active" value={stat?.counts.active ?? 0} />
        <StatCard label="quarantined" value={stat?.quarantined ?? 0} />
        <StatCard label="superseded" value={stat?.counts.superseded ?? 0} />
        <StatCard label="pinned" value={stat?.pinned ?? 0} />
        <StatCard label="embeddings" value={stat?.embeddings ?? 0} />
      </div>

      <div className="mb-3 flex items-center gap-2">
        <Button variant={tab === 'store' ? 'primary' : 'ghost'} onClick={() => setTab('store')}>
          Store
        </Button>
        <Button variant={tab === 'review' ? 'primary' : 'ghost'} onClick={() => setTab('review')}>
          Review queue{stat && stat.quarantined > 0 ? ` (${stat.quarantined})` : ''}
        </Button>
        {forgetEnabled && (
          <Button
            variant={tab === 'forgotten' ? 'primary' : 'ghost'}
            onClick={() => setTab('forgotten')}
          >
            Forgotten
          </Button>
        )}
      </div>

      {tab === 'forgotten' && <ForgottenSection />}

      {tab === 'store' && (
        <div className="mb-3 flex flex-wrap items-end gap-2">
          <TextInput
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="search text…"
            className="max-w-56"
          />
          <Select value={kind} onChange={(e) => setKind(e.target.value)} className="max-w-40">
            <option value="">all kinds</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </Select>
          <Select value={status} onChange={(e) => setStatus(e.target.value)} className="max-w-40">
            <option value="">all statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
          <div className="ml-auto flex items-end gap-2">
            <TextInput
              value={newText}
              onChange={(e) => setNewText(e.target.value)}
              placeholder="remember something new…"
              className="w-72"
            />
            <Select value={newKind} onChange={(e) => setNewKind(e.target.value)} className="max-w-36">
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
            <Button
              variant="primary"
              disabled={!newText.trim()}
              onClick={() => {
                void api
                  .post('/memories', { text: newText.trim(), kind: newKind })
                  .then(() => {
                    setNewText('')
                    invalidate('memories')
                  })
              }}
            >
              + Remember
            </Button>
          </div>
        </div>
      )}

      {tab !== 'forgotten' && (
      <div className="overflow-x-auto rounded-lg border border-slate-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-void-950/80 font-mono text-[10px] uppercase tracking-widest text-slate-500">
            <tr>
              <th className="px-3 py-2">Memory</th>
              <th className="px-3 py-2">Kind</th>
              <th className="px-3 py-2">Source</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Imp</th>
              <th className="px-3 py-2">Recorded</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr
                key={m.id}
                onClick={() => setSelected(m)}
                className="cursor-pointer border-t border-slate-800/60 transition-colors hover:bg-slate-900/60"
              >
                <td className="max-w-md px-3 py-2">
                  <span className="line-clamp-2 text-slate-200">
                    {m.pinned && <span title="pinned" className="mr-1 text-accent-300">◈</span>}
                    {m.text}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <KindBadge kind={m.kind} />
                </td>
                <td className="px-3 py-2 font-mono text-[11px] text-slate-400">{m.source}</td>
                <td className="px-3 py-2">
                  <StatusPill status={m.status} />
                </td>
                <td className="px-3 py-2 font-mono text-[11px] text-slate-400">{m.importance}</td>
                <td className="px-3 py-2 font-mono text-[11px] text-slate-500">
                  {new Date(m.recorded_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-sm text-slate-500">
                  {tab === 'review' ? 'nothing awaiting review' : 'no memories stored yet'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      )}

      <Drawer open={selected !== null} onClose={() => setSelected(null)} title="Memory detail">
        {selected && (
          <MemoryDetail row={selected} onDone={() => setSelected(null)} forgetEnabled={forgetEnabled} />
        )}
      </Drawer>
    </div>
  )
}
