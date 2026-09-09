import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useInvalidate, useRemoteAgents } from '../api/hooks'
import type { A2ATask, RemoteAgent } from '../api/types'
import { RegistryTable } from '../components/RegistryTable'
import {
  Button,
  Chip,
  Drawer,
  ErrorNote,
  Field,
  JsonBlock,
  PageHeader,
  SourceBadge,
  StaticNotice,
  StatusPill,
  TextInput,
  timeAgo,
} from '../components/ui'

// spec §8.10 — the Remote Agents page (§19): register an external A2A agent
// by Agent Card URL; its declared skills project into the Tools registry as
// kind='a2a'. Credentials are WRITE-ONLY: entered here, never displayed —
// the API only ever returns per-scheme configured flags.

export interface CredentialRow {
  scheme: string
  kind: 'secret' | 'oauth2'
  value: string
  clientId: string
  clientSecret: string
}

export const EMPTY_CRED: CredentialRow = {
  scheme: '',
  kind: 'secret',
  value: '',
  clientId: '',
  clientSecret: '',
}

/** Form rows → the API credentials payload ({} pruned to null). */
export function credentialsOut(
  rows: CredentialRow[],
): Record<string, unknown> | null {
  const out: Record<string, unknown> = {}
  for (const row of rows) {
    if (!row.scheme.trim()) continue
    if (row.kind === 'oauth2') {
      if (row.clientId || row.clientSecret) {
        out[row.scheme.trim()] = {
          client_id: row.clientId,
          client_secret: row.clientSecret,
        }
      }
    } else if (row.value) {
      out[row.scheme.trim()] = row.value
    }
  }
  return Object.keys(out).length ? out : null
}

export function AuthSchemeChips({ agent }: { agent: RemoteAgent }) {
  const entries = Object.entries(agent.auth ?? {})
  if (!entries.length) {
    return <span className="text-xs text-slate-500">no auth declared — open</span>
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([name, meta]) => (
        <span
          key={name}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] ring-1 ${
            !meta.supported
              ? 'text-rose-300 ring-rose-500/40'
              : meta.configured
                ? 'text-emerald-300 ring-emerald-500/40'
                : 'text-amber-300 ring-amber-500/40'
          }`}
          title={
            !meta.supported
              ? 'scheme not supported by this build'
              : meta.configured
                ? 'credentials configured'
                : 'no credentials stored'
          }
        >
          {name} · {meta.type}
          {!meta.supported ? ' ✕' : meta.configured ? ' ✓' : ' …'}
        </span>
      ))}
    </div>
  )
}

function CredentialsEditor({
  rows,
  onChange,
}: {
  rows: CredentialRow[]
  onChange: (rows: CredentialRow[]) => void
}) {
  const update = (i: number, patch: Partial<CredentialRow>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5" data-testid="credential-rows">
      {rows.map((row, i) => (
        <div key={i} className="space-y-1.5 rounded-md border border-slate-800 p-2">
          <div className="flex items-center gap-1.5">
            <TextInput
              placeholder="scheme name (from the card)"
              value={row.scheme}
              onChange={(e) => update(i, { scheme: e.target.value })}
              className="flex-1"
            />
            <select
              value={row.kind}
              onChange={(e) => update(i, { kind: e.target.value as CredentialRow['kind'] })}
              className="rounded-md border border-slate-700 bg-void-950/60 px-2 py-1.5 text-xs text-slate-200"
            >
              <option value="secret">secret / token</option>
              <option value="oauth2">oauth2 client</option>
            </select>
            <Button variant="ghost" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              ✕
            </Button>
          </div>
          {row.kind === 'secret' ? (
            <TextInput
              type="password"
              placeholder='secret, "user:pass" for basic, or env:VAR_NAME'
              value={row.value}
              onChange={(e) => update(i, { value: e.target.value })}
            />
          ) : (
            <div className="flex gap-1.5">
              <TextInput
                placeholder="client_id (or env:VAR)"
                value={row.clientId}
                onChange={(e) => update(i, { clientId: e.target.value })}
                className="flex-1"
              />
              <TextInput
                type="password"
                placeholder="client_secret (or env:VAR)"
                value={row.clientSecret}
                onChange={(e) => update(i, { clientSecret: e.target.value })}
                className="flex-1"
              />
            </div>
          )}
        </div>
      ))}
      <Button variant="ghost" onClick={() => onChange([...rows, { ...EMPTY_CRED }])}>
        + credential
      </Button>
    </div>
  )
}

function RegisterForm({ onDone }: { onDone: () => void }) {
  const invalidate = useInvalidate()
  const [cardUrl, setCardUrl] = useState('')
  const [name, setName] = useState('')
  const [creds, setCreds] = useState<CredentialRow[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.post('/remote-agents', {
        card_url: cardUrl.trim(),
        name: name.trim() || null,
        credentials: credentialsOut(creds),
      })
      invalidate('remote-agents', 'tools')
      onDone()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <Field
        label="Agent card URL"
        hint="base URL or the full /.well-known/agent-card.json path — fetched and validated on save"
      >
        <TextInput
          placeholder="https://agent.example.com"
          value={cardUrl}
          onChange={(e) => setCardUrl(e.target.value)}
        />
      </Field>
      <Field label="Name (optional)" hint="defaults to the card's declared name">
        <TextInput value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field
        label="Credentials"
        hint="one row per card security scheme — write-only, never displayed again; env:VAR reads from the backend environment"
      >
        <CredentialsEditor rows={creds} onChange={setCreds} />
      </Field>
      <ErrorNote error={error} />
      <Button variant="primary" onClick={() => void submit()} disabled={busy || !cardUrl.trim()}>
        {busy ? 'Fetching card…' : 'Register'}
      </Button>
    </div>
  )
}

function TasksSection({ agent }: { agent: RemoteAgent }) {
  // spec §19.6 — open/parked/recent remote tasks; reply continues an
  // input-required task, cancel propagates tasks/cancel
  const invalidate = useInvalidate()
  const { data: tasks = [] } = useQuery({
    queryKey: ['remote-agents', agent.id, 'tasks'],
    queryFn: () => api.get<A2ATask[]>(`/remote-agents/${agent.id}/tasks`),
    refetchInterval: 5000,
  })
  const [reply, setReply] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    setError(null)
    try {
      await fn()
      invalidate('remote-agents', 'deliveries')
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  if (!tasks.length) {
    return <span className="text-xs text-slate-500">no remote tasks yet</span>
  }
  return (
    <div className="space-y-2" data-testid="a2a-tasks">
      {tasks.map((t) => (
        <div key={t.id} className="rounded-md border border-slate-800 px-3 py-2">
          <div className="flex items-center gap-2">
            <StatusPill status={t.state} />
            <span className="font-mono text-[10px] text-slate-500">
              {t.remote_task_id ? t.remote_task_id.slice(0, 8) : '—'}
            </span>
            {t.delivered && <Chip tone="muted">delivered</Chip>}
            <span className="ml-auto font-mono text-[10px] text-slate-600">
              {timeAgo(t.updated_at)}
            </span>
          </div>
          {t.question && t.state === 'input-required' && (
            <div className="mt-2 space-y-1.5">
              <div className="rounded border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-200">
                asks (untrusted): {t.question}
              </div>
              <div className="flex gap-1.5">
                <TextInput
                  placeholder="reply to the remote agent…"
                  value={reply[t.id] ?? ''}
                  onChange={(e) => setReply({ ...reply, [t.id]: e.target.value })}
                  className="flex-1"
                />
                <Button
                  variant="primary"
                  disabled={busy !== null || !(reply[t.id] ?? '').trim()}
                  onClick={() =>
                    void act(`reply-${t.id}`, () =>
                      api.post(`/remote-agents/${agent.id}/tasks/${t.id}/reply`, {
                        text: (reply[t.id] ?? '').trim(),
                      }),
                    )
                  }
                >
                  {busy === `reply-${t.id}` ? 'Sending…' : 'Reply'}
                </Button>
              </div>
            </div>
          )}
          {t.error && <div className="mt-1 font-mono text-[10px] text-rose-300">{t.error}</div>}
          {(t.state === 'parked' || t.state === 'input-required' || t.state === 'working') && (
            <div className="mt-2">
              <Button
                variant="ghost"
                disabled={busy !== null}
                onClick={() =>
                  void act(`cancel-${t.id}`, () =>
                    api.post(`/remote-agents/${agent.id}/tasks/${t.id}/cancel`),
                  )
                }
              >
                {busy === `cancel-${t.id}` ? 'Cancelling…' : 'Cancel remote task'}
              </Button>
            </div>
          )}
        </div>
      ))}
      <ErrorNote error={error} />
    </div>
  )
}

interface CardSkill {
  id?: string
  name?: string
  description?: string
  tags?: string[]
}

function AgentDetail({ agent, onClose }: { agent: RemoteAgent; onClose: () => void }) {
  const invalidate = useInvalidate()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [creds, setCreds] = useState<CredentialRow[]>([])
  const isStatic = agent.source === 'static'
  const skills = ((agent.card?.skills as CardSkill[] | undefined) ?? []).filter(Boolean)

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    setError(null)
    try {
      await fn()
      invalidate('remote-agents', 'tools')
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill status={agent.status} title={agent.last_error ?? undefined} />
        <SourceBadge source={agent.source} />
        <Chip>{agent.tool_count} tools</Chip>
        <span className="font-mono text-[10px] text-slate-500">
          card fetched {timeAgo(agent.card_fetched_at)}
        </span>
      </div>
      {isStatic && <StaticNotice />}
      {agent.last_error && (
        <div className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 font-mono text-xs text-rose-300">
          {agent.last_error}
        </div>
      )}

      <Field label="Auth (per card scheme)">
        <AuthSchemeChips agent={agent} />
      </Field>

      <Field label="Declared skills — projected into Tools as kind=a2a">
        <div className="space-y-1.5">
          {skills.map((s, i) => (
            <div key={s.id ?? i} className="rounded-md border border-slate-800 px-3 py-2">
              <div className="text-sm font-medium text-slate-200">{s.name ?? s.id}</div>
              {s.description && <div className="text-xs text-slate-400">{s.description}</div>}
              {!!s.tags?.length && (
                <div className="mt-1 font-mono text-[10px] text-slate-500">
                  {s.tags.join(' · ')}
                </div>
              )}
            </div>
          ))}
          {!skills.length && <span className="text-xs text-slate-500">card declares no skills</span>}
        </div>
      </Field>

      <Field label="Remote tasks (§19.6)">
        <TasksSection agent={agent} />
      </Field>

      <Field
        label="Set credentials"
        hint="write-only — saving replaces the named schemes; existing values are never shown"
      >
        <CredentialsEditor rows={creds} onChange={setCreds} />
        <div className="mt-2">
          <Button
            disabled={busy !== null || !credentialsOut(creds)}
            onClick={() =>
              void act('creds', async () => {
                await api.patch(`/remote-agents/${agent.id}`, {
                  credentials: credentialsOut(creds),
                })
                setCreds([])
              })
            }
          >
            {busy === 'creds' ? 'Saving…' : 'Save credentials'}
          </Button>
        </div>
      </Field>

      <Field label="Agent card (as last fetched)">
        <JsonBlock value={agent.card} />
      </Field>

      <ErrorNote error={error} />
      <div className="flex flex-wrap gap-2 border-t border-slate-800 pt-4">
        <Button
          disabled={busy !== null}
          onClick={() =>
            void act('refresh', () => api.post(`/remote-agents/${agent.id}/refresh-card`))
          }
        >
          {busy === 'refresh' ? 'Refreshing…' : 'Refresh card'}
        </Button>
        {!isStatic && (
          <Button
            variant="danger"
            disabled={busy !== null}
            onClick={() =>
              void act('delete', async () => {
                await api.delete(`/remote-agents/${agent.id}`)
                onClose()
              })
            }
          >
            {busy === 'delete' ? 'Deleting…' : 'Delete'}
          </Button>
        )}
      </div>
    </div>
  )
}

export function RemoteAgentsPage() {
  const { data: agents = [], isLoading } = useRemoteAgents()
  const [registering, setRegistering] = useState(false)
  const [selected, setSelected] = useState<RemoteAgent | null>(null)
  const current = selected ? (agents.find((a) => a.id === selected.id) ?? selected) : null

  return (
    <div className="p-6">
      <PageHeader
        title="Remote Agents"
        subtitle="external A2A agents — registered by card URL, skills projected into Tools, auth read off the card (spec §19)"
        actions={
          <Button variant="primary" onClick={() => setRegistering(true)}>
            + Register agent
          </Button>
        }
      />
      <RegistryTable
        rows={agents}
        loading={isLoading}
        empty="no remote agents yet — register one by its Agent Card URL"
        onRowClick={(row) => setSelected(row)}
        filterRow={(row, q, source) =>
          (!q || row.name.toLowerCase().includes(q) || row.card_url.toLowerCase().includes(q)) &&
          (!source || row.source === source)
        }
        columns={[
          {
            header: 'Name',
            render: (row) => (
              <div>
                <div className="font-medium text-slate-200">{row.name}</div>
                <div className="font-mono text-[10px] text-slate-500">{row.card_url}</div>
              </div>
            ),
          },
          {
            header: 'Status',
            render: (row) => <StatusPill status={row.status} title={row.last_error ?? undefined} />,
          },
          {
            header: 'Auth',
            render: (row) => (
              <span
                className={`font-mono text-[11px] ${
                  row.auth_status === 'ok' || row.auth_status === 'open'
                    ? 'text-emerald-300'
                    : row.auth_status === 'unconfigured'
                      ? 'text-amber-300'
                      : 'text-rose-300'
                }`}
              >
                {row.auth_status}
              </span>
            ),
          },
          { header: 'Tools', render: (row) => <Chip>{row.tool_count}</Chip>, width: 'w-20' },
          {
            header: 'Card fetched',
            render: (row) => (
              <span className="text-xs text-slate-500">{timeAgo(row.card_fetched_at)}</span>
            ),
          },
        ]}
      />

      <Drawer open={registering} onClose={() => setRegistering(false)} title="Register remote agent">
        <RegisterForm onDone={() => setRegistering(false)} />
      </Drawer>
      <Drawer open={current !== null} onClose={() => setSelected(null)} title={current?.name ?? ''}>
        {current && <AgentDetail agent={current} onClose={() => setSelected(null)} />}
      </Drawer>
    </div>
  )
}
