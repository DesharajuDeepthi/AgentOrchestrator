/** Skills (spec §8.3): template-based skill document editor — frontmatter as
 * form fields, tool tags, model+effort override, markdown body with template,
 * side-by-side preview, {tool:...} mention validation at save. */
import { CacheControls } from '../components/CacheControls'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useInvalidate, useProviders, useSkills, useTools } from '../api/hooks'
import type { ModelParams, OverlapCheck, Skill, SubAgent, Tool } from '../api/types'
import { OverlapDialog } from '../components/OverlapDialog'
import { RegistryTable } from '../components/RegistryTable'
import { ExposureWarningBanner } from './ToolsPage'
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

const SKILL_TEMPLATE = `# Purpose

What this skill accomplishes.

## Steps

1. First step — reference a bound tool inline like {tool:server.tool_name}.
2. A pure reasoning step needs no tool.
3. Synthesize the final output.

## Output format

Describe exactly what this skill should return.
`

export function ModelOverrideFields({
  model,
  params,
  onModel,
  onParams,
  disabled,
}: {
  model: string | null
  params: ModelParams | null
  onModel: (m: string | null) => void
  onParams: (p: ModelParams | null) => void
  disabled?: boolean
}) {
  const { data: providers = [] } = useProviders()
  const options = providers.flatMap((p) =>
    p.models.map((m) => ({
      ref: `${p.provider_id}:${m.id}`,
      label: `${p.provider_id}:${m.id}${p.configured ? '' : ' (unconfigured)'}`,
      supports: m,
      configured: p.configured,
    })),
  )
  const selected = options.find((o) => o.ref === model)
  return (
    <div className="grid grid-cols-2 gap-3">
      <Field label="Model override" hint="null inherits sub agent, then defaults">
        <Select
          disabled={disabled}
          value={model ?? ''}
          onChange={(e) => {
            onModel(e.target.value || null)
            if (!e.target.value) onParams(null)
          }}
        >
          <option value="">(inherit)</option>
          {options.map((o) => (
            <option key={o.ref} value={o.ref} disabled={!o.configured}>
              {o.label}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Effort">
        <Select
          disabled={disabled || !model || !selected?.supports.supports_effort}
          value={params?.effort ?? ''}
          onChange={(e) =>
            onParams({ ...params, effort: (e.target.value || null) as ModelParams['effort'] })
          }
        >
          <option value="">(default)</option>
          {['none', 'low', 'medium', 'high'].map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Temperature">
        <TextInput
          type="number"
          step="0.1"
          min="0"
          max="2"
          disabled={disabled || !model || !selected?.supports.supports_temperature}
          value={params?.temperature ?? ''}
          onChange={(e) =>
            onParams({
              ...params,
              temperature: e.target.value === '' ? null : Number(e.target.value),
            })
          }
        />
      </Field>
      <Field label="Max output tokens">
        <TextInput
          type="number"
          disabled={disabled || !model || !selected?.supports.supports_max_output_tokens}
          value={params?.max_output_tokens ?? ''}
          onChange={(e) =>
            onParams({
              ...params,
              max_output_tokens: e.target.value === '' ? null : Number(e.target.value),
            })
          }
        />
      </Field>
    </div>
  )
}

function cleanParams(p: ModelParams | null): ModelParams | null {
  if (!p) return null
  const out: ModelParams = {}
  if (p.effort) out.effort = p.effort
  if (p.temperature !== null && p.temperature !== undefined) out.temperature = p.temperature
  if (p.max_output_tokens) out.max_output_tokens = p.max_output_tokens
  return Object.keys(out).length ? out : null
}

/** naive markdown-lite renderer for the live preview pane */
function MarkdownPreview({ text, boundKeys }: { text: string; boundKeys: string[] }) {
  const html = useMemo(() => {
    const lines = text.split('\n')
    return lines.map((line, i) => {
      const mention = /\{tool:([^}]+)\}/g
      const parts: React.ReactNode[] = []
      let last = 0
      let m: RegExpExecArray | null
      while ((m = mention.exec(line)) !== null) {
        parts.push(line.slice(last, m.index))
        const bound = boundKeys.includes(m[1].trim())
        parts.push(
          <code
            key={`${i}-${m.index}`}
            className={cx(
              'rounded px-1 text-[11px]',
              bound ? 'bg-indigo-500/20 text-indigo-300' : 'bg-rose-500/20 text-rose-300',
            )}
            title={bound ? 'bound tool' : 'NOT BOUND — save will reject'}
          >
            {m[1]}
          </code>,
        )
        last = m.index + m[0].length
      }
      parts.push(line.slice(last))
      if (line.startsWith('# '))
        return (
          <h1 key={i} className="mt-2 text-base font-bold text-slate-100">
            {line.slice(2)}
          </h1>
        )
      if (line.startsWith('## '))
        return (
          <h2 key={i} className="mt-2 text-sm font-semibold text-slate-200">
            {line.slice(3)}
          </h2>
        )
      return (
        <p key={i} className="min-h-4 text-xs leading-relaxed text-slate-400">
          {parts}
        </p>
      )
    })
  }, [text, boundKeys])
  return <div className="space-y-0.5">{html}</div>
}

function SkillEditor({
  skill,
  onDone,
}: {
  skill: Skill | null
  onDone: () => void
}) {
  const { data: tools = [] } = useTools()
  const invalidate = useInvalidate()
  const navigate = useNavigate()
  const [name, setName] = useState(skill?.name ?? '')
  const [description, setDescription] = useState(skill?.description ?? '')
  const [persona, setPersona] = useState(skill?.persona ?? '')
  const [exposure, setExposure] = useState(skill?.direct_exposure ?? false)
  const [maxIter, setMaxIter] = useState<string>(
    skill?.max_tool_iterations != null ? String(skill.max_tool_iterations) : '',
  )
  const [model, setModel] = useState<string | null>(skill?.model ?? null)
  const [params, setParams] = useState<ModelParams | null>(skill?.model_params ?? null)
  const [toolIds, setToolIds] = useState<string[]>(skill?.tools.map((t) => t.id) ?? [])
  const [instructions, setInstructions] = useState(skill?.instructions || SKILL_TEMPLATE)
  const [toolQuery, setToolQuery] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [overlap, setOverlap] = useState<OverlapCheck | null>(null)
  const isStatic = skill?.source === 'static'

  // system-seeded static tools first (spec §8.3)
  const sorted = [...tools].sort((a, b) =>
    a.source === b.source ? a.tool_key.localeCompare(b.tool_key) : a.source === 'static' ? -1 : 1,
  )
  const boundKeys = tools.filter((t) => toolIds.includes(t.id)).map((t) => t.tool_key)

  const doSave = async () => {
    const body = {
      name,
      description,
      persona,
      instructions,
      tool_ids: toolIds,
      direct_exposure: exposure,
      max_tool_iterations: maxIter === '' ? null : Number(maxIter),
      model,
      model_params: cleanParams(params),
    }
    try {
      if (skill) await api.patch(`/skills/${skill.id}`, body)
      else await api.post('/skills', body)
      invalidate('skills', 'tools')
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
      // NOTE: only the fields SkillOverlapCheck accepts — an extra field
      // 422s the advisory check and silently disables the guard (M40 fix)
      const check = await api.post<OverlapCheck>('/skills/check-overlap', {
        name,
        description,
        instructions,
        tool_ids: toolIds,
        exclude_id: skill?.id ?? null,
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

  return (
    <div className="space-y-4">
      {overlap && (
        <OverlapDialog
          check={overlap}
          entity="skill"
          onConfirm={async () => {
            // M44: saving past the warning is a captured, content-free event
            void api.post('/skills/overlap-ack', {
              draft_type: 'skill',
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
        <Field label="Expose to orchestrator">
          <Toggle
            checked={exposure}
            onChange={async (v) => {
              setExposure(v)
              if (skill) {
                await api.patch(`/skills/${skill.id}`, { direct_exposure: v })
                invalidate('skills')
              }
            }}
          />
        </Field>
      </div>
      <Field label="Description" hint="the planner routes by this prose — make it precise">
        <TextInput
          value={description}
          disabled={isStatic}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      <Field label="Persona (minor)">
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
      <Field
        label="Max tool iterations"
        hint="per-skill loop budget (spec §3.3) — empty inherits the settings default; web-research ships with 20"
      >
        <TextInput
          type="number"
          min={1}
          disabled={isStatic}
          value={maxIter}
          onChange={(e) => setMaxIter(e.target.value)}
          className="max-w-28"
          placeholder="default"
        />
      </Field>
      <Field label="Tool tags" hint="binding = availability, strictly — the loop sees only these">
        <TextInput
          placeholder="filter tools…"
          value={toolQuery}
          onChange={(e) => setToolQuery(e.target.value)}
          disabled={isStatic}
        />
        <div className="mt-2 max-h-44 space-y-1 overflow-y-auto rounded-md border border-slate-800 p-2">
          {sorted
            .filter(
              (t) =>
                t.status === 'active' &&
                (!toolQuery || t.tool_key.toLowerCase().includes(toolQuery.toLowerCase())),
            )
            .map((t) => (
              <label key={t.id} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  disabled={isStatic}
                  checked={toolIds.includes(t.id)}
                  onChange={(e) =>
                    setToolIds(
                      e.target.checked
                        ? [...toolIds, t.id]
                        : toolIds.filter((id) => id !== t.id),
                    )
                  }
                />
                <code className="text-slate-300">{t.tool_key}</code>
                <KindBadge kind={t.kind} />
                <SourceBadge source={t.source} />
              </label>
            ))}
        </div>
      </Field>
      <Field
        label="Instructions (markdown body)"
        hint="soft workflow: steps guide the loop; {tool:...} mentions must resolve to tagged tools"
      >
        <div className="grid grid-cols-2 gap-3">
          <TextArea
            rows={14}
            value={instructions}
            disabled={isStatic}
            onChange={(e) => setInstructions(e.target.value)}
          />
          <div className="max-h-80 overflow-y-auto rounded-md border border-slate-800 bg-slate-900/40 p-3">
            <MarkdownPreview text={instructions} boundKeys={boundKeys} />
          </div>
        </div>
      </Field>
      <ErrorNote error={error} />
      {isStatic && skill && (
        <div className="flex justify-end">
          <Button variant="ghost" onClick={() => navigate(`/evals?target=${skill.id}`)}>
            Evals →
          </Button>
        </div>
      )}
      {!isStatic && (
        <div className="flex justify-between">
          {skill ? (
            <Button
              variant="danger"
              onClick={async () => {
                setError(null)
                try {
                  await api.delete(`/skills/${skill.id}`)
                  invalidate('skills', 'tools')
                  onDone()
                } catch (e) {
                  setError(e) // 409 lists dependent sub agents
                }
              }}
            >
              Delete
            </Button>
          ) : (
            <span />
          )}
          <span className="flex gap-2">
            {skill && (
              <Button variant="ghost" onClick={() => navigate(`/evals?target=${skill.id}`)}>
                Evals →
              </Button>
            )}
            <Button variant="primary" onClick={save} disabled={!name}>
              {skill ? 'Save skill' : 'Create skill'}
            </Button>
          </span>
        </div>
      )}
    </div>
  )
}

function SubAgentChips({ skillId }: { skillId: string }) {
  const navigate = useNavigate()
  const { data: agents = [] } = useQuery({
    queryKey: ['skills', skillId, 'sub-agents'],
    queryFn: () => api.get<SubAgent[]>(`/skills/${skillId}/sub-agents`),
  })
  if (agents.length === 0) return <Chip tone="muted">none</Chip>
  return (
    <div className="flex flex-wrap gap-1">
      {agents.map((a) => (
        <Chip key={a.id} onClick={() => navigate(`/sub-agents?focus=${a.id}`)}>
          {a.name}
        </Chip>
      ))}
    </div>
  )
}

export function SkillsPage() {
  const { data: skills = [], isLoading } = useSkills()
  const [searchParams] = useSearchParams()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    const focus = searchParams.get('focus')
    if (focus) setSelectedId(focus)
  }, [searchParams])

  const selected = skills.find((s) => s.id === selectedId) ?? null
  return (
    <div className="p-6">
      <PageHeader
        title="Skills"
        subtitle="Markdown documents: minor persona + multi-step instructions + strict tool bindings."
        actions={
          <>
            <CacheControls registry="skills" />
            <Button variant="primary" onClick={() => setCreating(true)}>
              + New skill
            </Button>
          </>
        }
      />
      <ExposureWarningBanner />
      <RegistryTable
        rows={skills}
        loading={isLoading}
        kinds={['native', 'custom']}
        onRowClick={(s) => setSelectedId(s.id)}
        filterRow={(s, q, source, kind) =>
          (!q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)) &&
          (!source || s.source === source) &&
          (!kind || s.kind === kind)
        }
        columns={[
          {
            header: 'Skill',
            render: (s) => (
              <div className="flex items-center gap-2">
                <span className="font-medium text-slate-200">{s.name}</span>
                <KindBadge kind={s.kind} />
                <SourceBadge source={s.source} />
                {s.direct_exposure && <Chip tone="direct">direct</Chip>}
              </div>
            ),
          },
          {
            header: 'Persona',
            render: (s) => (
              <span className="line-clamp-1 max-w-xs text-xs text-slate-400">
                {s.persona.split('\n')[0]}
              </span>
            ),
          },
          {
            header: 'Tools',
            render: (s) => (
              <div className="flex flex-wrap gap-1">
                {s.tools.length === 0 && <Chip tone="muted">no tools</Chip>}
                {s.tools.slice(0, 4).map((t: Tool) => (
                  <Chip key={t.id} onClick={() => navigate(`/tools?focus=${t.id}`)}>
                    {t.tool_key}
                  </Chip>
                ))}
                {s.tools.length > 4 && <Chip tone="muted">+{s.tools.length - 4}</Chip>}
              </div>
            ),
          },
          { header: 'Sub agents', render: (s) => <SubAgentChips skillId={s.id} /> },
          { header: 'Status', render: (s) => <StatusPill status={s.status} /> },
        ]}
      />
      <Drawer
        open={creating}
        onClose={() => setCreating(false)}
        title="New skill document"
        wide
      >
        {creating && <SkillEditor skill={null} onDone={() => setCreating(false)} />}
      </Drawer>
      <Drawer open={selected !== null} onClose={() => setSelectedId(null)} title={selected?.name} wide>
        {selected && (
          <SkillEditor key={selected.id} skill={selected} onDone={() => setSelectedId(null)} />
        )}
      </Drawer>
    </div>
  )
}
