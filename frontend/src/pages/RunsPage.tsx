/** Runs (spec §8.6): mode badge, step timeline grouped by sub agent, nested
 * native-tool steps, route reasons, cancel / retry / delete. */
import { useState } from 'react'
import { api } from '../api/client'
import { useInvalidate, useRun, useRuns, useSubAgents } from '../api/hooks'
import type { Run, RunStep } from '../api/types'
import { RegistryTable } from '../components/RegistryTable'
import { AnswerTrace, type AnswerUiPayload } from '../components/AnswerPanel'
import {
  Button,
  Chip,
  Drawer,
  ErrorNote,
  Field,
  JsonBlock,
  KindBadge,
  PageHeader,
  StatusPill,
  cx,
  duration,
  timeAgo,
} from '../components/ui'

const STEP_ICONS: Record<string, string> = {
  plan: '🧭',
  route: '↳',
  skill: '⚙',
  hitl: '⏸',
  tool_call: '🛠',
  aggregate: 'Σ',
}

function StepRow({ step, depth }: { step: RunStep; depth: number }) {
  const [open, setOpen] = useState(false)
  const rung = step.step_type === 'route' ? (step.output?.rung as string | undefined) : undefined
  return (
    <div style={{ marginLeft: depth * 20 }}>
      <button
        onClick={() => setOpen(!open)}
        className={cx(
          'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-slate-900',
          step.status === 'failed' && 'bg-rose-500/5',
        )}
      >
        <span className="w-5 text-center">{STEP_ICONS[step.step_type] ?? '·'}</span>
        <span className="font-medium text-slate-300">{step.step_type}</span>
        {step.node_id && <code className="text-[10px] text-slate-500">{step.node_id}</code>}
        {rung && <Chip tone={rung === 'fallback' ? 'direct' : 'default'}>rung: {rung}</Chip>}
        {step.model && <code className="text-[10px] text-indigo-400">{step.model}</code>}
        <span className="ml-auto flex items-center gap-2 text-[10px] text-slate-500">
          {step.input_tokens + step.output_tokens > 0 && (
            <span>
              {step.input_tokens}→{step.output_tokens} tok
            </span>
          )}
          <span>{duration(step.started_at, step.finished_at)}</span>
          <StatusPill status={step.status} />
        </span>
      </button>
      {open && (
        <div className="ml-7 space-y-2 border-l border-slate-800 py-2 pl-3">
          {step.error && (
            <div className="rounded bg-rose-500/10 px-2 py-1 font-mono text-[11px] text-rose-300">
              {step.error}
            </div>
          )}
          {step.input != null && (
            <div>
              <div className="mb-1 text-[10px] uppercase text-slate-600">input</div>
              <JsonBlock value={step.input} />
            </div>
          )}
          {step.output != null && (
            <div>
              <div className="mb-1 text-[10px] uppercase text-slate-600">output</div>
              <JsonBlock value={step.output} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StepTree({ steps }: { steps: RunStep[] }) {
  const byParent = new Map<string | null, RunStep[]>()
  for (const s of steps) {
    const key = s.parent_step_id
    byParent.set(key, [...(byParent.get(key) ?? []), s])
  }
  const render = (parent: string | null, depth: number): React.ReactNode =>
    (byParent.get(parent) ?? []).map((s) => (
      <div key={s.id}>
        <StepRow step={s} depth={depth} />
        {render(s.id, depth + 1)}
      </div>
    ))
  return <div className="space-y-0.5">{render(null, 0)}</div>
}

function RunDetail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const { data: run } = useRun(runId)
  const { data: agents = [] } = useSubAgents()
  const invalidate = useInvalidate()
  const [error, setError] = useState<unknown>(null)
  if (!run) return <div className="text-sm text-slate-500">Loading…</div>

  const act = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      invalidate('runs')
    } catch (e) {
      setError(e)
    }
  }
  const agentsInvolved = [
    ...new Set(
      (run.steps ?? [])
        .filter((s) => s.sub_agent_id)
        .map((s) => agents.find((a) => a.id === s.sub_agent_id)?.name ?? 'ephemeral'),
    ),
  ]
  const pausedStep = (run.steps ?? []).find(
    (s) => s.step_type === 'hitl' && s.status === 'running',
  )
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill status={run.status} />
        <KindBadge kind={run.orchestrator_mode} />
        <span className="text-[11px] text-slate-500">
          {run.total_input_tokens}→{run.total_output_tokens} tokens ·{' '}
          {duration(run.started_at, run.finished_at)} · {timeAgo(run.started_at)}
        </span>
      </div>
      <Field label="Message">
        <p className="text-sm text-slate-300">{run.chat_message}</p>
      </Field>
      {run.error && (
        <div className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {run.error}
        </div>
      )}
      {run.status === 'paused_hitl' && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
          <div className="mb-2 text-xs font-medium text-amber-300">
            ⏸ Paused for human approval
            {pausedStep ? ` — ${JSON.stringify(pausedStep.input)}` : ''}
          </div>
          <div className="flex gap-2">
            <Button
              variant="primary"
              onClick={() =>
                act(() => api.post(`/runs/${run.id}/hitl`, { decision: 'approve', note: '' }))
              }
            >
              Approve
            </Button>
            <Button
              variant="danger"
              onClick={() =>
                act(() => api.post(`/runs/${run.id}/hitl`, { decision: 'deny', note: '' }))
              }
            >
              Deny
            </Button>
          </div>
        </div>
      )}
      {run.final_answer && (
        <Field label="Answer">
          <AnswerTrace
            markdown={run.final_answer}
            payload={run.answer_ui as AnswerUiPayload | null}
            toolCharts={run.charts}
          />
        </Field>
      )}
      {agentsInvolved.length > 0 && (
        <Field label="Sub agents involved">
          <div className="flex flex-wrap gap-1">
            {agentsInvolved.map((n) => (
              <Chip key={n}>{n}</Chip>
            ))}
          </div>
        </Field>
      )}
      <Field label="Step timeline">
        <StepTree steps={run.steps ?? []} />
      </Field>
      {run.plan != null && (
        <Field label="Plan JSON">
          <JsonBlock value={run.plan} />
        </Field>
      )}
      {run.snapshot != null && (
        <Field label="Config snapshot (frozen at dispatch)">
          <JsonBlock value={run.snapshot} />
        </Field>
      )}
      <ErrorNote error={error} />
      <div className="flex gap-2 border-t border-slate-800 pt-3">
        {run.status === 'running' && (
          <Button variant="danger" onClick={() => act(() => api.post(`/runs/${run.id}/cancel`))}>
            Cancel run
          </Button>
        )}
        {run.status === 'failed' && (
          <Button variant="primary" onClick={() => act(() => api.post(`/runs/${run.id}/retry`))}>
            Retry (re-plan)
          </Button>
        )}
        {run.status !== 'running' && (
          <Button
            variant="danger"
            onClick={() =>
              act(async () => {
                await api.delete(`/runs/${run.id}`)
                onClose()
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

export function RunsPage() {
  const { data: runs = [], isLoading } = useRuns()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  return (
    <div className="p-6">
      <PageHeader
        title="Runs"
        subtitle="Every run fully traced: plan, route reasons, per-step tokens, nested tool calls."
      />
      <RegistryTable
        rows={runs}
        loading={isLoading}
        onRowClick={(r) => setSelectedId(r.id)}
        filterRow={(r: Run, q) =>
          !q ||
          r.chat_message.toLowerCase().includes(q) ||
          r.status.includes(q) ||
          r.orchestrator_mode.includes(q)
        }
        columns={[
          {
            header: 'Time',
            render: (r) => <span className="text-xs text-slate-500">{timeAgo(r.started_at)}</span>,
          },
          {
            header: 'Message',
            render: (r) => (
              <span className="line-clamp-1 max-w-md text-xs text-slate-300">{r.chat_message}</span>
            ),
          },
          { header: 'Status', render: (r) => <StatusPill status={r.status} /> },
          { header: 'Mode', render: (r) => <KindBadge kind={r.orchestrator_mode} /> },
          {
            header: 'Duration',
            render: (r) => (
              <span className="text-xs text-slate-500">{duration(r.started_at, r.finished_at)}</span>
            ),
          },
          {
            header: 'Tokens',
            render: (r) => (
              <span className="text-xs text-slate-500">
                {r.total_input_tokens}→{r.total_output_tokens}
              </span>
            ),
          },
        ]}
      />
      <Drawer
        open={selectedId !== null}
        onClose={() => setSelectedId(null)}
        title="Run trace"
        wide
      >
        {selectedId && <RunDetail runId={selectedId} onClose={() => setSelectedId(null)} />}
      </Drawer>
    </div>
  )
}
