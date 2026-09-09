/** Tools (spec §8.2): skill badges (chips → skill detail), `direct` badge,
 * schema drawer, expose-to-orchestrator toggle. */
import { CacheControls } from '../components/CacheControls'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useInvalidate, useServers, useSettings, useSkills, useTools } from '../api/hooks'
import type { Skill, Tool } from '../api/types'
import { RegistryTable } from '../components/RegistryTable'
import {
  Button,
  Chip,
  Drawer,
  ErrorNote,
  Field,
  JsonBlock,
  KindBadge,
  PageHeader,
  SourceBadge,
  StaticNotice,
  StatusPill,
  TextInput,
  Toggle,
} from '../components/ui'

export function ExposureWarningBanner() {
  const { data: settings } = useSettings()
  const { data: tools = [] } = useTools()
  const { data: skills = [] } = useSkills()
  if (!settings) return null
  const exposed =
    tools.filter((t) => t.direct_exposure).length + skills.filter((s) => s.direct_exposure).length
  const cap = Number(settings.direct_exposure_cap_warning ?? 10)
  if (exposed <= cap) return null
  return (
    <div className="mb-4 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
      ⚠ {exposed} capabilities are directly exposed to the orchestrator (warning threshold {cap}).
      Every exposure adds planner context cost — prefer progressive disclosure through sub agents.
    </div>
  )
}

function SkillChips({ toolId }: { toolId: string }) {
  const navigate = useNavigate()
  const { data: skills = [] } = useQuery({
    queryKey: ['tools', toolId, 'skills'],
    queryFn: () => api.get<Skill[]>(`/tools/${toolId}/skills`),
  })
  if (skills.length === 0) return <Chip tone="muted">unassigned</Chip>
  return (
    <div className="flex flex-wrap gap-1">
      {skills.map((s) => (
        <Chip key={s.id} onClick={() => navigate(`/skills?focus=${s.id}`)}>
          {s.name}
        </Chip>
      ))}
    </div>
  )
}

function ToolDetail({ tool }: { tool: Tool }) {
  const { data: servers = [] } = useServers()
  const invalidate = useInvalidate()
  const [error, setError] = useState<unknown>(null)
  const [description, setDescription] = useState(tool.description)
  const server = servers.find((s) => s.id === tool.mcp_server_id)
  const isStatic = tool.source === 'static'

  const patch = async (body: Record<string, unknown>) => {
    setError(null)
    try {
      await api.patch(`/tools/${tool.id}`, body)
      invalidate('tools')
    } catch (e) {
      setError(e)
    }
  }

  return (
    <div className="space-y-4">
      {isStatic && <StaticNotice />}
      <div className="flex items-center gap-2">
        <KindBadge kind={tool.kind} />
        <SourceBadge source={tool.source} />
        <StatusPill status={tool.status} />
      </div>
      <Field label="Tool key">
        <code className="text-xs text-indigo-300">{tool.tool_key}</code>
      </Field>
      {server && (
        <Field label="Server">
          <span className="text-sm">{server.name}</span>
        </Field>
      )}
      {tool.native_ref && (
        <Field label="Native ref">
          <code className="text-xs text-emerald-300">{tool.native_ref}</code>
        </Field>
      )}
      <Field label="Description">
        <TextInput
          value={description}
          disabled={isStatic}
          onChange={(e) => setDescription(e.target.value)}
          onBlur={() => !isStatic && description !== tool.description && patch({ description })}
        />
      </Field>
      <Field label="Expose to orchestrator" hint="rung-1 direct tool call, no skill persona">
        <Toggle
          checked={tool.direct_exposure}
          onChange={(v) => patch({ direct_exposure: v })}
        />
      </Field>
      <Field label="Status">
        <Toggle
          checked={tool.status === 'active'}
          onChange={(v) => patch({ status: v ? 'active' : 'inactive' })}
          label={tool.status}
        />
      </Field>
      <Field label="Bound in skills">
        <SkillChips toolId={tool.id} />
      </Field>
      <Field label="Input schema (server-owned)">
        <JsonBlock value={tool.input_schema} />
      </Field>
      <ErrorNote error={error} />
    </div>
  )
}

export function ToolsPage() {
  const { data: tools = [], isLoading } = useTools()
  const { data: servers = [] } = useServers()
  const [selected, setSelected] = useState<Tool | null>(null)
  const serverName = (id: string | null) => servers.find((s) => s.id === id)?.name ?? '—'
  const current = selected ? (tools.find((t) => t.id === selected.id) ?? selected) : null
  return (
    <div className="p-6">
      <PageHeader
        title="Tools"
        subtitle="Ingested from MCP servers or registered natively — bindable to skills, never created here."
        actions={<CacheControls registry="tools" />}
      />
      <ExposureWarningBanner />
      <RegistryTable
        rows={tools}
        loading={isLoading}
        kinds={['mcp', 'native', 'a2a']}
        onRowClick={setSelected}
        filterRow={(t, q, source, kind) =>
          (!q ||
            t.tool_key.toLowerCase().includes(q) ||
            t.description.toLowerCase().includes(q)) &&
          (!source || t.source === source) &&
          (!kind || t.kind === kind)
        }
        columns={[
          {
            header: 'Tool',
            render: (t) => (
              <div className="flex items-center gap-2">
                <code className="text-xs font-medium text-slate-200">{t.tool_key}</code>
                <KindBadge kind={t.kind} />
                <SourceBadge source={t.source} />
                {t.direct_exposure && <Chip tone="direct">direct</Chip>}
              </div>
            ),
          },
          {
            header: 'Server',
            render: (t) => (
              <span className="text-xs text-slate-400">
                {t.kind === 'mcp' ? serverName(t.mcp_server_id) : 'native'}
              </span>
            ),
          },
          {
            header: 'Description',
            render: (t) => (
              <span className="line-clamp-1 max-w-md text-xs text-slate-400">{t.description}</span>
            ),
          },
          { header: 'Status', render: (t) => <StatusPill status={t.status} /> },
          { header: 'Skills', render: (t) => <SkillChips toolId={t.id} /> },
        ]}
      />
      <Drawer open={current !== null} onClose={() => setSelected(null)} title={current?.tool_key}>
        {current && <ToolDetail tool={current} />}
      </Drawer>
      <div className="mt-2">
        <Button variant="ghost" onClick={() => window.location.reload()}>
          ↻ refresh
        </Button>
      </div>
    </div>
  )
}
