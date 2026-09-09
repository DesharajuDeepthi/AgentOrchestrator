/** Sub Agents (spec §8.4): starter-template workflow builder (node list +
 * edge list) with a live read-only react-flow preview, inline validation,
 * native agents as definition cards, "Test invoke" into Chat. */
import { CacheControls } from '../components/CacheControls'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Background, Handle, Position, ReactFlow, type Edge, type Node } from '@xyflow/react'
import { api } from '../api/client'
import { useInvalidate, useRuns, useSkills, useSubAgents } from '../api/hooks'
import type {
  ModelParams,
  OverlapCheck,
  SubAgent,
  Workflow,
  WorkflowEdge,
  WorkflowNode,
} from '../api/types'
import { OverlapDialog } from '../components/OverlapDialog'
import { RegistryTable } from '../components/RegistryTable'
import { ModelOverrideFields } from './SkillsPage'
import {
  Button,
  Chip,
  Drawer,
  ErrorNote,
  Field,
  KindBadge,
  PageHeader,
  Select,
  SourceBadge,
  StaticNotice,
  StatusPill,
  TextArea,
  TextInput,
  Toggle,
  cx,
} from '../components/ui'

// ── starter templates (spec §8.4) ────────────────────────────────

const TEMPLATES: Record<string, (skillId: string) => Workflow> = {
  Blank: () => ({ nodes: [], edges: [] }),
  'Sequential pipeline': (skillId) => ({
    nodes: [
      { id: 'step-1', type: 'skill', skill_id: skillId },
      { id: 'step-2', type: 'skill', skill_id: skillId },
    ],
    edges: [
      { from: 'START', to: 'step-1' },
      { from: 'step-1', to: 'step-2' },
      { from: 'step-2', to: 'END' },
    ],
  }),
  'Branch + HITL approve': (skillId) => ({
    nodes: [
      { id: 'work', type: 'skill', skill_id: skillId },
      { id: 'approve', type: 'hitl', prompt: 'Approve the result?' },
      { id: 'finish', type: 'skill', skill_id: skillId },
    ],
    edges: [
      { from: 'START', to: 'work' },
      { from: 'work', to: 'approve', condition: 'if work produced a result' },
      { from: 'work', to: 'END', condition: 'if nothing to do' },
      { from: 'approve', to: 'finish' },
      { from: 'finish', to: 'END' },
    ],
  }),
  'Parallel fan-out/join': (skillId) => ({
    nodes: [
      { id: 'split', type: 'skill', skill_id: skillId },
      { id: 'left', type: 'skill', skill_id: skillId },
      { id: 'right', type: 'skill', skill_id: skillId },
      { id: 'join', type: 'skill', skill_id: skillId },
    ],
    edges: [
      { from: 'START', to: 'split' },
      { from: 'split', to: 'left' },
      { from: 'split', to: 'right' },
      { from: 'left', to: 'join' },
      { from: 'right', to: 'join' },
      { from: 'join', to: 'END' },
    ],
  }),
}

// ── react-flow preview ───────────────────────────────────────────

function FlowNode({ data }: { data: { label: string; kind: string; error?: boolean } }) {
  return (
    <div
      className={cx(
        'rounded-md border px-3 py-1.5 text-[11px] font-medium',
        data.error
          ? 'border-rose-500 bg-rose-500/10 text-rose-300'
          : data.kind === 'hitl'
            ? 'border-amber-500/50 bg-amber-500/10 text-amber-300'
            : data.kind === 'terminal'
              ? 'border-slate-600 bg-slate-800 text-slate-400'
              : 'border-indigo-500/50 bg-indigo-500/10 text-indigo-200',
      )}
    >
      <Handle type="target" position={Position.Top} className="!bg-slate-600" />
      {data.label}
      <Handle type="source" position={Position.Bottom} className="!bg-slate-600" />
    </div>
  )
}

const nodeTypes = { concierge: FlowNode }

function layout(workflow: Workflow): { nodes: Node[]; edges: Edge[] } {
  // simple layered layout: BFS depth from START — depth-capped so cycles or
  // self-loops mid-edit can never freeze the preview
  const depth: Record<string, number> = { START: 0 }
  const queue = ['START']
  const maxDepth = workflow.nodes.length + 2
  while (queue.length) {
    const current = queue.shift()!
    for (const e of workflow.edges.filter((e) => e.from === current)) {
      if (e.to === e.from) continue
      const d = (depth[current] ?? 0) + 1
      if (d > maxDepth) continue
      if (depth[e.to] === undefined || d > depth[e.to]) {
        depth[e.to] = d
        if (e.to !== 'END') queue.push(e.to)
      }
    }
  }
  const byDepth: Record<number, string[]> = {}
  const all = ['START', ...workflow.nodes.map((n) => n.id), 'END']
  for (const id of all) {
    const d = depth[id] ?? Object.keys(byDepth).length
    byDepth[d] = [...(byDepth[d] ?? []), id]
  }
  const nodes: Node[] = all.map((id) => {
    const d = depth[id] ?? 0
    const row = byDepth[d] ?? [id]
    const idx = row.indexOf(id)
    const node = workflow.nodes.find((n) => n.id === id)
    return {
      id,
      type: 'concierge',
      position: { x: 60 + idx * 180 - (row.length - 1) * 90 + 200, y: 30 + d * 90 },
      data: {
        label: id === 'START' || id === 'END' ? id : `${node?.type === 'hitl' ? '⏸ ' : ''}${id}`,
        kind: id === 'START' || id === 'END' ? 'terminal' : (node?.type ?? 'skill'),
      },
    }
  })
  const edges: Edge[] = workflow.edges.map((e, i) => ({
    id: `e${i}`,
    source: e.from,
    target: e.to,
    label: e.on === 'error' ? '⚠ on error' : e.condition,
    animated: e.on === 'error',
    style: e.on === 'error' ? { stroke: '#f43f5e' } : { stroke: '#6366f1' },
    labelStyle: { fill: '#94a3b8', fontSize: 9 },
    labelBgStyle: { fill: '#0f172a' },
  }))
  return { nodes, edges }
}

export function WorkflowPreview({ workflow }: { workflow: Workflow }) {
  const { nodes, edges } = useMemo(() => layout(workflow), [workflow])
  return (
    <div className="h-72 rounded-lg border border-slate-800 bg-slate-900/40">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        colorMode="dark"
      >
        <Background gap={16} color="#1e293b" />
      </ReactFlow>
    </div>
  )
}

// ── builder ──────────────────────────────────────────────────────

function AgentEditor({ agent, onDone }: { agent: SubAgent | null; onDone: () => void }) {
  const { data: skills = [] } = useSkills()
  const invalidate = useInvalidate()
  const navigate = useNavigate()
  const activeSkills = skills.filter((s) => s.status === 'active' && !s.deleted_at)
  const [name, setName] = useState(agent?.name ?? '')
  const [description, setDescription] = useState(agent?.description ?? '')
  const [persona, setPersona] = useState(agent?.persona ?? '')
  const [model, setModel] = useState<string | null>(agent?.model ?? null)
  const [params, setParams] = useState<ModelParams | null>(agent?.model_params ?? null)
  const [workflow, setWorkflow] = useState<Workflow>(agent?.workflow ?? { nodes: [], edges: [] })
  const [exposure, setExposure] = useState(agent?.direct_exposure ?? false)
  const [template, setTemplate] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [validation, setValidation] = useState<string[] | null>(null)
  const [overlap, setOverlap] = useState<OverlapCheck | null>(null)
  const isStatic = agent?.source === 'static'

  const applyTemplate = (t: string) => {
    setTemplate(t)
    if (TEMPLATES[t] && activeSkills[0]) setWorkflow(TEMPLATES[t](activeSkills[0].id))
  }

  const setNode = (i: number, patch: Partial<WorkflowNode>) =>
    setWorkflow({
      ...workflow,
      nodes: workflow.nodes.map((n, j) => (j === i ? { ...n, ...patch } : n)),
    })
  const setEdge = (i: number, patch: Partial<WorkflowEdge>) =>
    setWorkflow({
      ...workflow,
      edges: workflow.edges.map((e, j) => (j === i ? { ...e, ...patch } : e)),
    })

  const doSave = async () => {
    const body = {
      name,
      description,
      persona,
      model,
      model_params: params && Object.keys(params).length ? params : null,
      workflow,
      direct_exposure: exposure,
    }
    try {
      if (agent) await api.patch(`/sub-agents/${agent.id}`, body)
      else await api.post('/sub-agents', body)
      invalidate('sub-agents', 'skills')
      onDone()
    } catch (e) {
      setError(e)
    }
  }

  const save = async () => {
    setError(null)
    // pre-save overlap guard (spec §4) — advisory, so check failures fall
    // through to a normal save
    try {
      const check = await api.post<OverlapCheck>('/sub-agents/check-overlap', {
        name,
        description,
        skill_ids: workflow.nodes.filter((n) => n.skill_id).map((n) => n.skill_id),
        exclude_id: agent?.id ?? null,
      })
      if (check.overlap) {
        setOverlap(check)
        return
      }
    } catch {
      // judge unavailable — never block the save on it
    }
    await doSave()
  }

  const validate = async () => {
    if (!agent) return
    const result = await api.post<{ valid: boolean; errors: string[] }>(
      `/sub-agents/${agent.id}/validate`,
    )
    setValidation(result.valid ? [] : result.errors)
  }

  const nodeIds = ['START', ...workflow.nodes.map((n) => n.id), 'END']

  return (
    <div className="space-y-4">
      {overlap && (
        <OverlapDialog
          check={overlap}
          entity="sub agent"
          onConfirm={async () => {
            // M44: saving past the warning is a captured, content-free event
            void api.post('/skills/overlap-ack', {
              draft_type: 'sub_agent',
              overlap_percent: overlap.overlap_percent,
            }).catch(() => {})
            setOverlap(null)
            await doSave()
          }}
          onCancel={() => setOverlap(null)}
        />
      )}
      {isStatic && <StaticNotice />}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <TextInput value={name} disabled={isStatic} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Starter template">
          <Select
            value={template}
            disabled={isStatic}
            onChange={(e) => applyTemplate(e.target.value)}
          >
            <option value="">(keep current)</option>
            {Object.keys(TEMPLATES).map((t) => (
              <option key={t}>{t}</option>
            ))}
          </Select>
        </Field>
      </div>
      <Field label="Description" hint="sub agent card prose — stage-1 selection depends on it">
        <TextInput
          value={description}
          disabled={isStatic}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      <Field
        label="Direct invocation"
        hint="exposed agents can be pinned from chat or POST /sub-agents/{id}/invoke — live on static records"
      >
        <Toggle
          checked={exposure}
          onChange={async (v) => {
            setExposure(v)
            if (agent) {
              await api.patch(`/sub-agents/${agent.id}`, { direct_exposure: v })
              invalidate('sub-agents')
            }
          }}
        />
      </Field>
      <Field label="Persona">
        <TextArea
          rows={2}
          value={persona}
          disabled={isStatic}
          onChange={(e) => setPersona(e.target.value)}
        />
      </Field>
      <ModelOverrideFields
        model={model}
        params={params}
        onModel={setModel}
        onParams={setParams}
        disabled={isStatic}
      />

      <Field label="Workflow — nodes">
        <div className="space-y-1.5">
          {workflow.nodes.map((n, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <TextInput
                value={n.id}
                disabled={isStatic}
                className="max-w-28"
                onChange={(e) => setNode(i, { id: e.target.value })}
              />
              <Select
                value={n.type}
                disabled={isStatic}
                className="max-w-24"
                onChange={(e) => setNode(i, { type: e.target.value as 'skill' | 'hitl' })}
              >
                <option value="skill">skill</option>
                <option value="hitl">hitl</option>
              </Select>
              {n.type === 'skill' ? (
                <Select
                  value={n.skill_id ?? ''}
                  disabled={isStatic}
                  onChange={(e) => setNode(i, { skill_id: e.target.value })}
                >
                  <option value="">pick skill…</option>
                  {activeSkills.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.tools.map((t) => t.tool_key).join(', ') || 'no tools'})
                    </option>
                  ))}
                </Select>
              ) : (
                <TextInput
                  placeholder="approval prompt"
                  disabled={isStatic}
                  value={n.prompt ?? ''}
                  onChange={(e) => setNode(i, { prompt: e.target.value })}
                />
              )}
              {!isStatic && (
                <Button
                  variant="ghost"
                  onClick={() =>
                    setWorkflow({
                      ...workflow,
                      nodes: workflow.nodes.filter((_, j) => j !== i),
                    })
                  }
                >
                  ✕
                </Button>
              )}
            </div>
          ))}
          {!isStatic && (
            <div className="flex gap-2">
              <Button
                onClick={() =>
                  setWorkflow({
                    ...workflow,
                    nodes: [
                      ...workflow.nodes,
                      {
                        id: `n${workflow.nodes.length + 1}`,
                        type: 'skill',
                        skill_id: activeSkills[0]?.id,
                      },
                    ],
                  })
                }
              >
                + skill node
              </Button>
              <Button
                onClick={() =>
                  setWorkflow({
                    ...workflow,
                    nodes: [
                      ...workflow.nodes,
                      { id: `h${workflow.nodes.length + 1}`, type: 'hitl', prompt: 'Approve?' },
                    ],
                  })
                }
              >
                + HITL node
              </Button>
            </div>
          )}
        </div>
      </Field>

      <Field label="Workflow — edges" hint="condition = natural language; on=error routes failures">
        <div className="space-y-1.5">
          {workflow.edges.map((e, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <Select
                value={e.from}
                disabled={isStatic}
                className="max-w-28"
                onChange={(ev) => setEdge(i, { from: ev.target.value })}
              >
                {nodeIds.filter((x) => x !== 'END').map((id) => (
                  <option key={id}>{id}</option>
                ))}
              </Select>
              <span className="text-slate-600">→</span>
              <Select
                value={e.to}
                disabled={isStatic}
                className="max-w-28"
                onChange={(ev) => setEdge(i, { to: ev.target.value })}
              >
                {nodeIds.filter((x) => x !== 'START').map((id) => (
                  <option key={id}>{id}</option>
                ))}
              </Select>
              <TextInput
                placeholder="condition (optional)"
                disabled={isStatic}
                value={e.condition ?? ''}
                onChange={(ev) => setEdge(i, { condition: ev.target.value || undefined })}
              />
              <Select
                value={e.on ?? 'success'}
                disabled={isStatic}
                className="max-w-24"
                onChange={(ev) =>
                  setEdge(i, { on: ev.target.value === 'error' ? 'error' : undefined })
                }
              >
                <option value="success">success</option>
                <option value="error">error</option>
              </Select>
              {!isStatic && (
                <Button
                  variant="ghost"
                  onClick={() =>
                    setWorkflow({ ...workflow, edges: workflow.edges.filter((_, j) => j !== i) })
                  }
                >
                  ✕
                </Button>
              )}
            </div>
          ))}
          {!isStatic && (
            <Button
              onClick={() =>
                setWorkflow({
                  ...workflow,
                  edges: [...workflow.edges, { from: 'START', to: workflow.nodes[0]?.id ?? 'END' }],
                })
              }
            >
              + edge
            </Button>
          )}
        </div>
      </Field>

      <Field label="Live preview (read-only)">
        <WorkflowPreview workflow={workflow} />
      </Field>

      {validation !== null && (
        <div
          className={cx(
            'rounded-md border px-3 py-2 text-xs',
            validation.length === 0
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
              : 'border-rose-500/30 bg-rose-500/10 text-rose-300',
          )}
        >
          {validation.length === 0 ? (
            '✓ compiles cleanly'
          ) : (
            <ul className="list-disc pl-4">
              {validation.map((v, i) => (
                <li key={i}>{v}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      <ErrorNote error={error} />
      <div className="flex justify-between gap-2">
        {agent && !isStatic ? (
          <Button
            variant="danger"
            onClick={async () => {
              setError(null)
              try {
                await api.delete(`/sub-agents/${agent.id}`)
                invalidate('sub-agents', 'skills')
                onDone()
              } catch (e) {
                setError(e)
              }
            }}
          >
            Delete
          </Button>
        ) : (
          <span />
        )}
        <div className="flex gap-2">
          {agent && (
            <Button variant="ghost" onClick={() => navigate(`/evals?target=${agent.id}`)}>
              Evals →
            </Button>
          )}
          {agent && <Button onClick={validate}>Validate (dry-run compile)</Button>}
          {!isStatic && (
            <Button variant="primary" onClick={save} disabled={!name}>
              {agent ? 'Save sub agent' : 'Create sub agent'}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

function NativeAgentCard({ agent }: { agent: SubAgent }) {
  const { data: skills = [] } = useSkills()
  const invalidate = useInvalidate()
  const [exposure, setExposure] = useState(agent.direct_exposure)
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-[11px] text-emerald-300">
        Native sub agent — the graph is code; the worker factory is bypassed. Read-only except
        status and direct exposure.
      </div>
      <Field label="Direct invocation" hint="exposed agents can be pinned from chat">
        <Toggle
          checked={exposure}
          onChange={async (v) => {
            setExposure(v)
            await api.patch(`/sub-agents/${agent.id}`, { direct_exposure: v })
            invalidate('sub-agents')
          }}
        />
      </Field>
      <Field label="Description">
        <p className="text-sm text-slate-300">{agent.description}</p>
      </Field>
      <Field label="Native ref">
        <code className="text-xs text-emerald-300">{agent.native_ref}</code>
      </Field>
      <Field label="Covers skills">
        <div className="flex flex-wrap gap-1">
          {(agent.covers_skill_ids ?? []).map((id) => (
            <Chip key={id}>{skills.find((s) => s.id === id)?.name ?? id.slice(0, 8)}</Chip>
          ))}
          {(agent.covers_skill_ids ?? []).length === 0 && <Chip tone="muted">none declared</Chip>}
        </div>
      </Field>
    </div>
  )
}

export function SubAgentsPage() {
  const { data: agents = [], isLoading } = useSubAgents()
  const { data: runs = [] } = useRuns()
  const { data: skills = [] } = useSkills()
  const [searchParams] = useSearchParams()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    const focus = searchParams.get('focus')
    if (focus) setSelectedId(focus)
  }, [searchParams])

  const selected = agents.find((a) => a.id === selectedId) ?? null
  const runCount = (agentId: string) =>
    runs.filter((r) => JSON.stringify(r.plan ?? {}).includes(agentId)).length

  return (
    <div className="p-6">
      <PageHeader
        title="Sub Agents"
        subtitle="Persona + hard workflow DAG over skills — compiled by the worker factory, validated at save."
        actions={
          <>
            <CacheControls registry="sub_agents" />
            <Button variant="primary" onClick={() => setCreating(true)}>
              + New sub agent
            </Button>
          </>
        }
      />
      <RegistryTable
        rows={agents}
        loading={isLoading}
        kinds={['native', 'custom']}
        onRowClick={(a) => setSelectedId(a.id)}
        filterRow={(a, q, source, kind) =>
          (!q || a.name.toLowerCase().includes(q) || a.description.toLowerCase().includes(q)) &&
          (!source || a.source === source) &&
          (!kind || a.kind === kind)
        }
        columns={[
          {
            header: 'Sub agent',
            render: (a) => (
              <div className="flex items-center gap-2">
                <span className="font-medium text-slate-200">{a.name}</span>
                <KindBadge kind={a.kind} />
                <SourceBadge source={a.source} />
                {a.direct_exposure && <Chip tone="direct">direct</Chip>}
              </div>
            ),
          },
          {
            header: 'Persona',
            render: (a) => (
              <span className="line-clamp-1 max-w-xs text-xs text-slate-400">
                {a.persona.split('\n')[0] || '—'}
              </span>
            ),
          },
          {
            header: 'Model',
            render: (a) => <code className="text-[11px] text-slate-400">{a.model ?? 'default'}</code>,
          },
          {
            header: 'Skills',
            render: (a) => (
              <div className="flex flex-wrap gap-1">
                {a.kind === 'native'
                  ? (a.covers_skill_ids ?? []).map((id) => (
                      <Chip key={id} onClick={() => navigate(`/skills?focus=${id}`)}>
                        {skills.find((s) => s.id === id)?.name ?? id.slice(0, 8)}
                      </Chip>
                    ))
                  : a.skills.map((s) => (
                      <Chip key={s.id} onClick={() => navigate(`/skills?focus=${s.id}`)}>
                        {s.name}
                      </Chip>
                    ))}
              </div>
            ),
          },
          {
            header: 'Runs',
            render: (a) => <span className="text-xs text-slate-500">{runCount(a.id)}</span>,
          },
          { header: 'Status', render: (a) => <StatusPill status={a.status} /> },
          {
            header: '',
            render: (a) =>
              a.status === 'active' && a.direct_exposure ? (
                <Button
                  variant="ghost"
                  onClick={() => navigate(`/?target=${a.id}`)}
                  title="opens Chat pinned to this sub agent — the next message runs as a direct run, planner bypassed"
                >
                  Invoke →
                </Button>
              ) : null,
          },
        ]}
      />
      <Drawer open={creating} onClose={() => setCreating(false)} title="New sub agent" wide>
        {creating && <AgentEditor agent={null} onDone={() => setCreating(false)} />}
      </Drawer>
      <Drawer
        open={selected !== null}
        onClose={() => setSelectedId(null)}
        title={selected?.name}
        wide
      >
        {selected &&
          (selected.kind === 'native' ? (
            <NativeAgentCard agent={selected} />
          ) : (
            <AgentEditor key={selected.id} agent={selected} onDone={() => setSelectedId(null)} />
          ))}
      </Drawer>
    </div>
  )
}
