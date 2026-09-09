/** MCP Servers (spec §8.1): register stdio/http, status pills with last_error
 * tooltips, masked env/header values, reconnect / refresh tools / delete. */
import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useInvalidate, useServers } from '../api/hooks'
import type { McpServer } from '../api/types'
import { RegistryTable } from '../components/RegistryTable'
import {
  Button,
  Drawer,
  ErrorNote,
  Field,
  MaskedValue,
  PageHeader,
  SourceBadge,
  StaticNotice,
  StatusPill,
  TextInput,
  timeAgo,
} from '../components/ui'

interface KV {
  key: string
  value: string
}

function KVEditor({
  label,
  items,
  onChange,
}: {
  label: string
  items: KV[]
  onChange: (items: KV[]) => void
}) {
  return (
    <Field label={label}>
      <div className="space-y-1.5">
        {items.map((kv, i) => (
          <div key={i} className="flex gap-1.5">
            <TextInput
              placeholder="KEY"
              value={kv.key}
              onChange={(e) =>
                onChange(items.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))
              }
            />
            <TextInput
              placeholder="value"
              type="password"
              value={kv.value}
              onChange={(e) =>
                onChange(items.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))
              }
            />
            <Button variant="ghost" onClick={() => onChange(items.filter((_, j) => j !== i))}>
              ✕
            </Button>
          </div>
        ))}
        <Button variant="ghost" onClick={() => onChange([...items, { key: '', value: '' }])}>
          + add
        </Button>
      </div>
    </Field>
  )
}

function RegisterForm({ onDone }: { onDone: () => void }) {
  const [transport, setTransport] = useState<'stdio' | 'http'>('stdio')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [url, setUrl] = useState('')
  const [env, setEnv] = useState<KV[]>([])
  const [headers, setHeaders] = useState<KV[]>([])
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const invalidate = useInvalidate()

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.post('/mcp-servers', {
        name,
        description,
        transport,
        command: transport === 'stdio' ? command : null,
        args: transport === 'stdio' && args.trim() ? args.trim().split(/\s+/) : null,
        env:
          transport === 'stdio' && env.length
            ? Object.fromEntries(env.filter((e) => e.key).map((e) => [e.key, e.value]))
            : null,
        url: transport === 'http' ? url : null,
        headers:
          transport === 'http' && headers.length
            ? Object.fromEntries(headers.filter((h) => h.key).map((h) => [h.key, h.value]))
            : null,
      })
      invalidate('mcp-servers', 'tools')
      onDone()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <Field label="Transport">
        <div className="flex gap-2">
          {(['stdio', 'http'] as const).map((t) => (
            <Button
              key={t}
              variant={transport === t ? 'primary' : 'secondary'}
              onClick={() => setTransport(t)}
            >
              {t}
            </Button>
          ))}
        </div>
      </Field>
      <Field label="Name">
        <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="my-server" />
      </Field>
      <Field label="Description">
        <TextInput
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="what this server provides — the planner reads this"
        />
      </Field>
      {transport === 'stdio' ? (
        <>
          <Field label="Command">
            <TextInput
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder="uvx"
            />
          </Field>
          <Field label="Args" hint="space-separated">
            <TextInput
              value={args}
              onChange={(e) => setArgs(e.target.value)}
              placeholder="mcp-server-fetch"
            />
          </Field>
          <KVEditor label="Env" items={env} onChange={setEnv} />
        </>
      ) : (
        <>
          <Field label="URL">
            <TextInput
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://host:8080/mcp"
            />
          </Field>
          <KVEditor label="Headers" items={headers} onChange={setHeaders} />
        </>
      )}
      <ErrorNote error={error} />
      <div className="flex justify-end gap-2">
        <Button variant="primary" onClick={submit} disabled={busy || !name}>
          {busy ? 'Connecting…' : 'Register & connect'}
        </Button>
      </div>
    </div>
  )
}

function ServerDetail({ server, onClose }: { server: McpServer; onClose: () => void }) {
  const invalidate = useInvalidate()
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    setError(null)
    try {
      await fn()
      invalidate('mcp-servers', 'tools')
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }
  const isStatic = server.source === 'static'
  return (
    <div className="space-y-4">
      {isStatic && <StaticNotice />}
      <div className="flex items-center gap-2">
        <StatusPill status={server.status} title={server.last_error ?? undefined} />
        <SourceBadge source={server.source} />
        <span className="text-[11px] text-slate-500">
          last connected {timeAgo(server.last_connected_at)}
        </span>
      </div>
      {server.last_error && (
        <div className="rounded-md border border-rose-500/20 bg-rose-500/5 px-3 py-2 font-mono text-[11px] text-rose-300">
          {server.last_error}
        </div>
      )}
      <Field label="Transport">
        <div className="text-sm">{server.transport}</div>
      </Field>
      {server.transport === 'stdio' ? (
        <Field label="Command">
          <code className="text-xs text-slate-300">
            {server.command} {(server.args ?? []).join(' ')}
          </code>
        </Field>
      ) : (
        <Field label="URL">
          <code className="text-xs text-slate-300">{server.url}</code>
        </Field>
      )}
      {server.env && Object.keys(server.env).length > 0 && (
        <Field label="Env (masked — click to reveal)">
          <div className="space-y-1">
            {Object.entries(server.env).map(([k, v]) => (
              <div key={k} className="flex items-center gap-2 text-xs">
                <code className="text-slate-400">{k}=</code>
                <MaskedValue value={String(v)} />
              </div>
            ))}
          </div>
        </Field>
      )}
      {server.headers && Object.keys(server.headers).length > 0 && (
        <Field label="Headers (masked — click to reveal)">
          <div className="space-y-1">
            {Object.entries(server.headers).map(([k, v]) => (
              <div key={k} className="flex items-center gap-2 text-xs">
                <code className="text-slate-400">{k}:</code>
                <MaskedValue value={String(v)} />
              </div>
            ))}
          </div>
        </Field>
      )}
      <ErrorNote error={error} />
      <div className="flex flex-wrap gap-2 border-t border-slate-800 pt-4">
        <Button
          onClick={() => act('reconnect', () => api.post(`/mcp-servers/${server.id}/reconnect`))}
          disabled={busy !== null}
        >
          {busy === 'reconnect' ? 'Reconnecting…' : 'Reconnect'}
        </Button>
        <Button
          onClick={() => act('refresh', () => api.post(`/mcp-servers/${server.id}/refresh-tools`))}
          disabled={busy !== null || server.status !== 'active'}
        >
          {busy === 'refresh' ? 'Refreshing…' : 'Refresh tools'}
        </Button>
        <Button
          onClick={() =>
            act('status', () =>
              api.patch(`/mcp-servers/${server.id}`, {
                status: server.status === 'active' ? 'inactive' : 'active',
              }),
            )
          }
          disabled={busy !== null}
        >
          {server.status === 'active' ? 'Deactivate' : 'Activate'}
        </Button>
        {!isStatic && (
          <Button
            variant="danger"
            disabled={busy !== null}
            onClick={() =>
              act('delete', async () => {
                try {
                  await api.delete(`/mcp-servers/${server.id}`)
                  onClose()
                } catch (e) {
                  if (e instanceof ApiError && e.status === 409) throw e
                  throw e
                }
              })
            }
          >
            Delete
          </Button>
        )}
      </div>
    </div>
  )
}

export function McpServersPage() {
  const { data: servers = [], isLoading } = useServers()
  const [selected, setSelected] = useState<McpServer | null>(null)
  const [registering, setRegistering] = useState(false)
  const current = selected ? (servers.find((s) => s.id === selected.id) ?? selected) : null
  return (
    <div className="p-6">
      <PageHeader
        title="MCP Servers"
        subtitle="Plug servers at runtime — tools ingest automatically and stay reconciled via listChanged."
        actions={
          <Button variant="primary" onClick={() => setRegistering(true)}>
            + Register server
          </Button>
        }
      />
      <RegistryTable
        rows={servers}
        loading={isLoading}
        filterRow={(s, q, source) =>
          (!q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)) &&
          (!source || s.source === source)
        }
        onRowClick={setSelected}
        columns={[
          {
            header: 'Name',
            render: (s) => (
              <div className="flex items-center gap-2">
                <span className="font-medium text-slate-200">{s.name}</span>
                <SourceBadge source={s.source} />
              </div>
            ),
          },
          { header: 'Transport', render: (s) => <code className="text-xs">{s.transport}</code> },
          {
            header: 'Status',
            render: (s) => <StatusPill status={s.status} title={s.last_error ?? undefined} />,
          },
          { header: 'Tools', render: (s) => <span className="text-slate-400">{s.tool_count}</span> },
          {
            header: 'Last connected',
            render: (s) => (
              <span className="text-xs text-slate-500">{timeAgo(s.last_connected_at)}</span>
            ),
          },
        ]}
      />
      <Drawer
        open={registering}
        onClose={() => setRegistering(false)}
        title="Register MCP server"
      >
        <RegisterForm onDone={() => setRegistering(false)} />
      </Drawer>
      <Drawer open={current !== null} onClose={() => setSelected(null)} title={current?.name}>
        {current && <ServerDetail server={current} onClose={() => setSelected(null)} />}
      </Drawer>
    </div>
  )
}
