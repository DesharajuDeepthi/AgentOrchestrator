import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { useInvalidate } from '../api/hooks'
import {
  Button,
  Chip,
  Drawer,
  EmptyState,
  Field,
  PageHeader,
  StatusPill,
  TextInput,
  timeAgo,
} from '../components/ui'

// spec §15 (M32) — the Evals page: upload csv/xlsx datasets, run them
// admin-direct against a skill or sub agent, read graded results.

interface DatasetRow {
  id: string
  name: string
  level: string
  target_id: string
  target_name: string | null
  case_count: number
  created_at: string | null
}

interface CaseRow {
  id: string
  input: string
  expected: string
  judge_notes: string
  grader: string
}

interface EvalRunRow {
  id: string
  dataset_id: string
  status: string
  total_cases: number
  passed_cases: number
  failed_cases: number
  error_cases: number
  langsmith_url: string | null
  started_at: string | null
  finished_at: string | null
}

interface ResultRow {
  id: string
  input: string
  expected: string
  grader: string
  run_id: string | null
  status: string
  passed: boolean
  score: number
  reason: string
  answer: string
}

async function uploadDataset(file: File, name: string): Promise<DatasetRow> {
  const form = new FormData()
  form.append('file', file)
  if (name.trim()) form.append('name', name.trim())
  const resp = await fetch('/api/v1/evals/datasets', { method: 'POST', body: form })
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as { detail?: unknown }
    throw new Error(typeof body.detail === 'string' ? body.detail : resp.statusText)
  }
  return (await resp.json()) as DatasetRow
}

function PassChip({ result }: { result: ResultRow }) {
  if (result.status === 'error') {
    return (
      <span className="rounded-full bg-amber-500/15 px-2 py-0.5 font-mono text-[10px] uppercase text-amber-300 ring-1 ring-amber-500/40">
        error
      </span>
    )
  }
  return result.passed ? (
    <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 font-mono text-[10px] uppercase text-emerald-300 ring-1 ring-emerald-500/40">
      pass
    </span>
  ) : (
    <span className="rounded-full bg-rose-500/15 px-2 py-0.5 font-mono text-[10px] uppercase text-rose-300 ring-1 ring-rose-500/40">
      fail
    </span>
  )
}

function RunResults({ evalRunId }: { evalRunId: string }) {
  const { data } = useQuery({
    queryKey: ['eval-run', evalRunId],
    queryFn: () => api.get<EvalRunRow & { results: ResultRow[] }>(`/evals/runs/${evalRunId}`),
    refetchInterval: 3000,
  })
  if (!data) return null
  return (
    <div className="space-y-3" data-testid="eval-results">
      <div className="flex items-center gap-2">
        <StatusPill status={data.status} />
        <span className="font-mono text-xs text-slate-300">
          {data.passed_cases}/{data.total_cases} passed
        </span>
        {data.failed_cases > 0 && (
          <span className="font-mono text-xs text-rose-300">{data.failed_cases} failed</span>
        )}
        {data.error_cases > 0 && (
          <span className="font-mono text-xs text-amber-300">{data.error_cases} errors</span>
        )}
        {data.langsmith_url && (
          <a
            href={data.langsmith_url}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-[10px] text-accent-300 underline"
          >
            LangSmith ↗
          </a>
        )}
      </div>
      <div className="space-y-2">
        {(data.results ?? []).map((r) => (
          <div key={r.id} className="rounded-lg border border-slate-800 bg-void-950/50 px-3 py-2">
            <div className="flex items-center gap-2">
              <PassChip result={r} />
              <Chip>{r.grader}</Chip>
              <span className="font-mono text-[10px] text-slate-500">score {r.score.toFixed(2)}</span>
              <span className="ml-auto truncate font-mono text-[10px] text-slate-600">
                run {r.run_id?.slice(0, 8) ?? '—'}
              </span>
            </div>
            <div className="mt-1 text-xs text-slate-300">{r.input}</div>
            <div className="mt-1 text-[11px] text-slate-500">
              expected: {r.expected || '(criteria)'} · answer: {r.answer.slice(0, 160) || '—'}
            </div>
            <div className="mt-0.5 text-[11px] italic text-slate-400">{r.reason}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function DatasetDetail({ dataset, onClose }: { dataset: DatasetRow; onClose: () => void }) {
  const invalidate = useInvalidate()
  const [activeRun, setActiveRun] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { data: detail } = useQuery({
    queryKey: ['eval-dataset', dataset.id],
    queryFn: () => api.get<DatasetRow & { cases: CaseRow[] }>(`/evals/datasets/${dataset.id}`),
  })
  const { data: runs } = useQuery({
    queryKey: ['eval-runs', dataset.id],
    queryFn: () => api.get<{ items: EvalRunRow[] }>(`/evals/runs?dataset_id=${dataset.id}`),
    refetchInterval: 3000,
  })
  const launch = async () => {
    setError(null)
    try {
      const out = await api.post<{ id: string }>(`/evals/datasets/${dataset.id}/run`)
      setActiveRun(out.id)
      invalidate('eval-runs')
    } catch (e) {
      setError(String(e))
    }
  }
  const remove = async () => {
    await api.delete(`/evals/datasets/${dataset.id}`)
    invalidate('eval-datasets')
    onClose()
  }
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Chip>{dataset.level}</Chip>
        <span className="text-sm text-slate-200">{dataset.target_name ?? dataset.target_id}</span>
        <span className="font-mono text-[10px] text-slate-500">{dataset.case_count} cases</span>
        <span className="ml-auto flex gap-2">
          <Button onClick={() => void launch()}>Run eval</Button>
          <Button variant="danger" onClick={() => void remove()}>
            Delete
          </Button>
        </span>
      </div>
      {error && <div className="text-xs text-rose-300">{error}</div>}
      <Field label="cases">
        <div className="max-h-44 space-y-1 overflow-y-auto">
          {(detail?.cases ?? []).map((c) => (
            <div
              key={c.id}
              className="flex items-center gap-2 rounded border border-slate-800/70 bg-void-950/50 px-2.5 py-1.5 text-xs"
            >
              <Chip>{c.grader}</Chip>
              <span className="flex-1 truncate text-slate-300">{c.input}</span>
              <span className="truncate text-slate-500">→ {c.expected}</span>
            </div>
          ))}
        </div>
      </Field>
      <Field label="eval runs">
        <div className="space-y-1">
          {(runs?.items ?? []).map((r) => (
            <div
              key={r.id}
              onClick={() => setActiveRun(r.id)}
              className={`flex cursor-pointer items-center gap-2 rounded border px-2.5 py-1.5 text-xs ${
                activeRun === r.id
                  ? 'border-accent-400/60 bg-accent-500/5'
                  : 'border-slate-800/70 bg-void-950/50 hover:bg-slate-900/50'
              }`}
            >
              <StatusPill status={r.status} />
              <span className="font-mono text-slate-300">
                {r.passed_cases}/{r.total_cases} passed
              </span>
              <span className="ml-auto font-mono text-[10px] text-slate-600">
                {timeAgo(r.started_at)}
              </span>
            </div>
          ))}
          {(runs?.items ?? []).length === 0 && (
            <div className="text-xs text-slate-600">never run — click “Run eval”</div>
          )}
        </div>
      </Field>
      {activeRun && <RunResults evalRunId={activeRun} />}
    </div>
  )
}

export function EvalsPage() {
  const invalidate = useInvalidate()
  const [searchParams] = useSearchParams()
  const targetFilter = searchParams.get('target')
  const fileRef = useRef<HTMLInputElement>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<DatasetRow | null>(null)
  const { data } = useQuery({
    queryKey: ['eval-datasets'],
    queryFn: () => api.get<{ items: DatasetRow[] }>('/evals/datasets'),
  })
  const items = (data?.items ?? []).filter((d) => !targetFilter || d.target_id === targetFilter)

  const upload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setError('choose a .csv or .xlsx file first')
      return
    }
    setError(null)
    try {
      const dataset = await uploadDataset(file, name)
      setName('')
      if (fileRef.current) fileRef.current.value = ''
      invalidate('eval-datasets')
      setSelected(dataset)
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <div className="p-6">
      <PageHeader
        title="Evals"
        subtitle="graded datasets run admin-direct against a skill or sub agent — every case is an ordinary run tagged eval=true"
      />
      <div className="mb-4 rounded-lg border border-slate-800 bg-void-950/40 p-3" data-testid="eval-upload">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          upload dataset — csv/xlsx with level,target_id,input,expected[,judge_notes][,grader]
        </div>
        <div className="flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.xlsx"
            className="text-xs text-slate-400 file:mr-2 file:rounded file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-xs file:text-slate-200"
          />
          <TextInput
            placeholder="dataset name (optional)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="max-w-56"
          />
          <Button onClick={() => void upload()}>Upload</Button>
        </div>
        {error && <div className="mt-2 text-xs text-rose-300">{error}</div>}
      </div>
      {targetFilter && (
        <div className="mb-2 text-xs text-slate-500">
          showing datasets for target <span className="font-mono">{targetFilter.slice(0, 8)}</span>
        </div>
      )}
      <div className="overflow-hidden rounded-lg border border-slate-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-void-950/70 font-mono text-[10px] uppercase tracking-widest text-slate-500">
            <tr>
              <th className="px-3 py-2">dataset</th>
              <th className="px-3 py-2">level</th>
              <th className="px-3 py-2">target</th>
              <th className="px-3 py-2">cases</th>
              <th className="px-3 py-2">created</th>
            </tr>
          </thead>
          <tbody>
            {items.map((d) => (
              <tr
                key={d.id}
                onClick={() => setSelected(d)}
                className="cursor-pointer border-t border-slate-800/60 hover:bg-slate-900/60"
              >
                <td className="px-3 py-2 font-medium text-slate-200">{d.name}</td>
                <td className="px-3 py-2">
                  <Chip>{d.level}</Chip>
                </td>
                <td className="px-3 py-2 text-slate-400">{d.target_name ?? d.target_id.slice(0, 8)}</td>
                <td className="px-3 py-2 font-mono text-xs text-slate-400">{d.case_count}</td>
                <td className="px-3 py-2 text-xs text-slate-500">{timeAgo(d.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && <EmptyState>no eval datasets yet — upload one above</EmptyState>}
      </div>
      <Drawer open={!!selected} onClose={() => setSelected(null)} title={selected?.name ?? ''} wide>
        {selected && <DatasetDetail dataset={selected} onClose={() => setSelected(null)} />}
      </Drawer>
    </div>
  )
}
