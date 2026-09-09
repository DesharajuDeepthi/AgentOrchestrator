/** Chat (spec §8.5): SSE streaming with event-driven instrument cards —
 * graph-mode plan cards with parallel groups, agentic live todo checklist,
 * dispatch rails, HITL approve/deny cards, fallback banner, A2UI answers. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, streamRun } from '../api/client'
import {
  useConversation,
  useConversations,
  useInvalidate,
  useSettings,
  useSubAgents,
} from '../api/hooks'
import type { SseEvent } from '../api/types'
import { AnswerBlock, type AnswerUiPayload } from '../components/AnswerPanel'
import { Button, Select, StatusPill, TextArea, cx, timeAgo } from '../components/ui'

type LiveEvent = Pick<SseEvent, 'type' | 'payload'>

interface PlanEntry {
  id: string
  capability: { type: string; id?: string; skill_ids?: string[] }
  task: string
  depends_on: string[]
}

interface Todo {
  content: string
  status: string
}

// ── instrument cards ─────────────────────────────────────────────

function PlanCard({ payload }: { payload: Record<string, unknown> }) {
  const entries = (payload.entries as PlanEntry[] | undefined) ?? []
  const todos = payload.todos as Todo[] | undefined
  if (todos) {
    return (
      <EventShell tone="accent" label="live plan · agentic todos">
        <ul className="space-y-1">
          {todos.map((t, i) => (
            <li key={i} className="flex items-center gap-2 text-xs">
              <span
                className={cx(
                  'grid size-4 place-items-center rounded border font-mono text-[9px]',
                  t.status === 'completed'
                    ? 'border-accent-500 bg-accent-500/20 text-accent-300'
                    : t.status === 'in_progress'
                      ? 'animate-pulse border-accent-400 text-accent-400'
                      : 'border-slate-700 text-slate-600',
                )}
              >
                {t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '▸' : ''}
              </span>
              <span
                className={cx(
                  t.status === 'completed' ? 'text-slate-500 line-through' : 'text-slate-300',
                )}
              >
                {t.content}
              </span>
            </li>
          ))}
        </ul>
      </EventShell>
    )
  }
  if (entries.length === 0) return null
  // group parallel waves by dependency depth
  const depth = new Map<string, number>()
  const resolve = (e: PlanEntry): number => {
    if (depth.has(e.id)) return depth.get(e.id)!
    const d = e.depends_on.length
      ? Math.max(
          ...e.depends_on.map((dep) => {
            const parent = entries.find((x) => x.id === dep)
            return parent ? resolve(parent) + 1 : 0
          }),
        )
      : 0
    depth.set(e.id, d)
    return d
  }
  entries.forEach(resolve)
  const waves = [...new Set([...depth.values()])].sort()
  return (
    <EventShell tone="accent" label="plan · graph mode">
      <div className="space-y-2">
        {waves.map((w) => {
          const wave = entries.filter((e) => depth.get(e.id) === w)
          return (
            <div key={w} className="flex items-start gap-2">
              <span className="mt-1 font-mono text-[9px] uppercase text-slate-600">
                {w === 0 ? 'wave 1' : `wave ${w + 1}`}
              </span>
              <div className="flex flex-1 flex-wrap gap-1.5">
                {wave.map((e) => (
                  <div
                    key={e.id}
                    className="rounded-md border border-accent-500/25 bg-accent-500/5 px-2.5 py-1.5"
                  >
                    <div className="font-mono text-[9px] uppercase tracking-wider text-accent-400">
                      {e.id} · {e.capability.type}
                    </div>
                    <div className="mt-0.5 max-w-72 text-[11px] leading-snug text-slate-300">
                      {e.task}
                    </div>
                    {e.depends_on.length > 0 && (
                      <div className="mt-0.5 font-mono text-[9px] text-slate-500">
                        after {e.depends_on.join(', ')}
                      </div>
                    )}
                  </div>
                ))}
                {wave.length > 1 && (
                  <span className="self-center font-mono text-[9px] uppercase text-slate-600">
                    ∥ parallel
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </EventShell>
  )
}

function RouteCard({ payload }: { payload: Record<string, unknown> }) {
  const rung = String(payload.rung ?? '')
  const resolved = payload.resolved_to as Record<string, unknown> | undefined
  if (rung === 'fallback') {
    return (
      <div className="animate-rise rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2">
        <div className="font-display text-[11px] font-semibold uppercase tracking-wider text-amber-300">
          ⚠ full-catalog fallback engaged
        </div>
        <p className="mt-0.5 text-[11px] text-amber-200/70">
          Descriptions didn't route this — the orchestrator is handling it itself with every
          active tool and skill.{' '}
          <Link to="/runs" className="underline">
            see route step
          </Link>
        </p>
      </div>
    )
  }
  return (
    <div className="animate-rise flex items-center gap-2 px-1 font-mono text-[10px] text-slate-500">
      <span className="text-accent-500">↳</span>
      <span className="uppercase tracking-wider">route</span>
      <span className="rounded bg-slate-800 px-1.5 py-0.5 text-accent-300">{rung}</span>
      {resolved?.entity_name != null && (
        <span className="text-slate-400">→ {String(resolved.entity_name)}</span>
      )}
    </div>
  )
}

// one nested line inside a dispatch group: a skill node, tool call, or gate
// the sub agent is running right now (spec §8.5 — grouped under its owner)
type ChildStep = {
  id: string
  parent: string | null
  stepType: string
  name: string | null
  nodeId: string | null
  status: string
}

function ChildStepLine({ step }: { step: ChildStep }) {
  const failed = step.status === 'failed'
  const done = step.status !== 'running'
  return (
    <div className="flex items-center gap-2 pl-4 font-mono text-[10px] uppercase tracking-wider">
      <span className={failed ? 'text-rose-400' : done ? 'text-slate-500' : 'text-accent-400'}>
        {failed ? '✕' : done ? '■' : '▶'}
      </span>
      <span className="text-slate-500">{step.stepType}</span>
      <span className={failed ? 'text-rose-300' : done ? 'text-slate-400' : 'text-accent-300'}>
        {step.name ?? step.nodeId ?? ''}
      </span>
      <span className="ml-auto text-slate-600">{failed ? 'failed' : done ? 'done' : '…'}</span>
    </div>
  )
}

function DispatchCard({
  payload,
  done,
  failed,
  children,
}: {
  payload: Record<string, unknown>
  done: boolean
  failed: boolean
  children?: React.ReactNode
}) {
  return (
    <div
      className={cx(
        'animate-rise rounded-md border px-3 py-2',
        failed
          ? 'border-rose-500/30 bg-rose-500/5'
          : done
            ? 'border-slate-800 bg-void-900/60'
            : 'border-accent-500/30 bg-accent-500/5',
      )}
    >
      <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider">
        <span className={failed ? 'text-rose-400' : done ? 'text-slate-500' : 'text-accent-400'}>
          {failed ? '✕' : done ? '■' : '▶'} {String(payload.tier ?? 'dispatch')}
        </span>
        {payload.kind != null && (
          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
            {String(payload.kind)}
          </span>
        )}
        {payload.entity_name != null && (
          <span className={failed ? 'text-rose-300' : done ? 'text-slate-400' : 'text-accent-300'}>
            {String(payload.entity_name)}
          </span>
        )}
        <span className="ml-auto text-slate-600">
          {failed ? 'failed' : done ? 'complete' : 'running'}
        </span>
      </div>
      {children != null && <div className="mt-2 space-y-1.5">{children}</div>}
      {!done && !failed && <div className="sweep-track mt-2" />}
    </div>
  )
}

function HitlCard({
  payload,
  runId,
  resolved,
  onResolved,
}: {
  payload: Record<string, unknown>
  runId: string
  resolved: boolean
  onResolved: () => void
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  interface GateQuestion {
    id: string
    prompt?: string
    kind: 'approve' | 'choice' | 'text'
    options?: string[]
  }
  const questions = (payload.questions as GateQuestion[] | null) ?? []
  const isForm = questions.length > 0
  const allAnswered = questions.every((q) => (answers[q.id] ?? '') !== '')
  const decide = async (decision: 'approve' | 'deny') => {
    setBusy(true)
    try {
      await api.post(`/runs/${runId}/hitl`, {
        decision,
        note,
        ...(isForm && decision === 'approve' ? { answers } : {}),
      })
      onResolved()
    } catch {
      // 409: the run is no longer paused — this gate was resolved through
      // another surface (HITL queue, cancel). Collapse rather than staying
      // armed-but-dead.
      onResolved()
    } finally {
      setBusy(false)
    }
  }
  const setAnswer = (id: string, value: string) =>
    setAnswers((a) => ({ ...a, [id]: value }))
  return (
    <div className="animate-rise rounded-lg border border-amber-500/50 bg-amber-500/10 p-3 shadow-[0_0_24px_-8px_var(--gate-glow)]">
      <div className="font-display flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-amber-300">
        <span className="animate-blink">⏸</span> human approval required
      </div>
      <p className="mt-1.5 text-sm text-amber-100">{String(payload.prompt ?? 'Approve?')}</p>
      {!resolved && isForm && (
        <div className="mt-2.5 space-y-2.5">
          {questions.map((q) => (
            <div key={q.id}>
              <div className="mb-1 font-mono text-[10px] tracking-wider text-amber-300 uppercase">
                {q.prompt || q.id}
              </div>
              {q.kind === 'text' ? (
                <input
                  value={answers[q.id] ?? ''}
                  onChange={(e) => setAnswer(q.id, e.target.value)}
                  placeholder="type your answer…"
                  className="w-full rounded-md border border-amber-500/30 bg-void-950/60 px-2.5 py-1.5 text-xs text-amber-100 placeholder:text-amber-500/40 focus:border-amber-400 focus:outline-none"
                />
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {(q.kind === 'approve' ? ['yes', 'no'] : (q.options ?? [])).map((opt) => (
                    <button
                      key={opt}
                      onClick={() => setAnswer(q.id, opt)}
                      className={cx(
                        'rounded-md border px-2.5 py-1 text-xs transition-colors',
                        answers[q.id] === opt
                          ? 'border-amber-400 bg-amber-500/25 text-amber-100'
                          : 'border-amber-500/30 text-amber-200/70 hover:border-amber-400/60',
                      )}
                    >
                      {opt}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {!resolved ? (
        <div className="mt-2.5 space-y-2">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="optional note for the worker…"
            className="w-full rounded-md border border-amber-500/30 bg-void-950/60 px-2.5 py-1.5 text-xs text-amber-100 placeholder:text-amber-500/40 focus:border-amber-400 focus:outline-none"
          />
          <div className="flex gap-2">
            <Button
              variant="primary"
              disabled={busy || (isForm && !allAnswered)}
              onClick={() => decide('approve')}
            >
              {isForm ? '✓ Submit answers' : '✓ Approve'}
            </Button>
            <Button variant="danger" disabled={busy} onClick={() => decide('deny')}>
              ✕ Deny
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-2 font-mono text-[10px] uppercase tracking-wider text-amber-500/70">
          resolved — resuming from checkpoint…
        </div>
      )}
    </div>
  )
}

function EventShell({
  children,
  label,
  tone,
}: {
  children: React.ReactNode
  label: string
  tone: 'accent' | 'muted'
}) {
  return (
    <div
      className={cx(
        'animate-rise rounded-lg border p-3',
        tone === 'accent' ? 'border-accent-500/20 bg-void-900/70' : 'border-slate-800 bg-void-900/50',
      )}
    >
      <div className="mb-2 font-mono text-[9px] uppercase tracking-[0.2em] text-slate-500">
        {label}
      </div>
      {children}
    </div>
  )
}

// ── live run rendering ───────────────────────────────────────────

function LiveRun({
  runId,
  onDone,
  onStatus,
}: {
  runId: string
  onDone: () => void
  onStatus?: (status: string) => void
}) {
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [tokens, setTokens] = useState('')
  const [thinking, setThinking] = useState('')
  const [active, setActive] = useState<Record<string, string>>({})
  const [steps, setSteps] = useState<Record<string, ChildStep>>({})
  const [hitlResolved, setHitlResolved] = useState(false)

  useEffect(() => {
    setEvents([])
    setTokens('')
    setThinking('')
    setActive({})
    setSteps({})
    const stop = streamRun(
      runId,
      (event) => {
        if (event.type === 'token') {
          setTokens((t) => t + String(event.payload.text ?? ''))
        } else if (event.type === 'thinking') {
          setThinking((t) => t + String(event.payload.text ?? ''))
        } else if (event.type === 'activity') {
          // live ticker: what the run is doing right now (payloads stay in traces)
          const p = event.payload
          setActive((a) => {
            const next = { ...a }
            const id = String(p.step_id)
            if (p.status === 'running') {
              const what = String(p.entity_name ?? p.node_id ?? '')
              next[id] = what ? `${String(p.step_type)} · ${what}` : String(p.step_type)
            } else {
              delete next[id]
            }
            return next
          })
          // step registry with lineage: children render nested under their
          // owning dispatch rail (spec §8.5 grouping)
          setSteps((s) => {
            const id = String(p.step_id)
            if (p.status === 'running' && p.step_type != null) {
              return {
                ...s,
                [id]: {
                  id,
                  parent: p.parent_step_id != null ? String(p.parent_step_id) : null,
                  stepType: String(p.step_type),
                  name: p.entity_name != null ? String(p.entity_name) : null,
                  nodeId: p.node_id != null ? String(p.node_id) : null,
                  status: 'running',
                },
              }
            }
            const prev = s[id]
            if (!prev) return s
            return { ...s, [id]: { ...prev, status: String(p.status ?? 'completed') } }
          })
        } else {
          setEvents((es) => [...es, event])
          if (event.type === 'hitl_request') setHitlResolved(false)
          if (event.type === 'run_status') onStatus?.(String(event.payload.status))
        }
      },
      onDone,
    )
    return stop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  const ticker = Object.values(active).slice(-3)

  const dispatchState = useMemo(() => {
    const ended = new Map<string, string>()
    for (const e of events)
      if (e.type === 'dispatch_end')
        ended.set(String(e.payload.step_id), String(e.payload.status ?? 'completed'))
    return ended
  }, [events])

  // keep only the latest plan card (todo list updates supersede)
  const lastPlanIdx = events.map((e) => e.type).lastIndexOf('plan')
  const lastHitlIdx = events.map((e) => e.type).lastIndexOf('hitl_request')
  // a run_status event AFTER the newest gate means that gate was consumed —
  // whether via this card, the Settings HITL queue, a cancel, or a resume
  // replay. Never leave armed buttons on a gate the backend already closed.
  const gateConsumed =
    lastHitlIdx >= 0 &&
    events.some(
      (e, idx) =>
        idx > lastHitlIdx &&
        e.type === 'run_status' &&
        String(e.payload.status) !== 'paused_hitl',
    )
  const gateResolved = hitlResolved || gateConsumed
  // the active gate's owning dispatch step — nests the card under its rail
  const hitlTarget =
    lastHitlIdx >= 0 && events[lastHitlIdx].payload.step_id != null
      ? String(events[lastHitlIdx].payload.step_id)
      : null
  const railIds = useMemo(
    () =>
      new Set(
        events.filter((e) => e.type === 'dispatch_start').map((e) => String(e.payload.step_id)),
      ),
    [events],
  )
  const childrenOf = (railId: string): ChildStep[] =>
    Object.values(steps).filter(
      (s) =>
        s.parent === railId &&
        s.id !== railId &&
        ['skill', 'tool_call', 'hitl'].includes(s.stepType),
    )

  return (
    <div className="space-y-2">
      {events.map((event, i) => {
        switch (event.type) {
          case 'plan':
            return i === lastPlanIdx ? <PlanCard key={i} payload={event.payload} /> : null
          case 'route':
            return <RouteCard key={i} payload={event.payload} />
          case 'dispatch_start': {
            const stepId = String(event.payload.step_id)
            const status = dispatchState.get(stepId)
            const kids = childrenOf(stepId)
            const ownGate = hitlTarget === stepId
            return (
              <DispatchCard
                key={i}
                payload={event.payload}
                done={status !== undefined}
                failed={status === 'failed'}
              >
                {kids.length > 0 || ownGate ? (
                  <>
                    {kids.map((s) => (
                      <ChildStepLine key={s.id} step={s} />
                    ))}
                    {ownGate && (
                      <div className="pl-4">
                        <HitlCard
                          payload={events[lastHitlIdx].payload}
                          runId={runId}
                          resolved={gateResolved}
                          onResolved={() => setHitlResolved(true)}
                        />
                      </div>
                    )}
                  </>
                ) : null}
              </DispatchCard>
            )
          }
          case 'hitl_request':
            // gates with a known owner render nested inside that rail above;
            // ownerless gates (root-level capabilities) render here
            return i === lastHitlIdx && (hitlTarget == null || !railIds.has(hitlTarget)) ? (
              <HitlCard
                key={i}
                payload={event.payload}
                runId={runId}
                resolved={gateResolved}
                onResolved={() => setHitlResolved(true)}
              />
            ) : null
          case 'error':
            return (
              <div
                key={i}
                className="animate-rise rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
              >
                ✕ {String(event.payload.message ?? 'error')}
              </div>
            )
          case 'run_status': {
            const status = String(event.payload.status)
            if (status === 'running' && i > 0)
              return (
                <div key={i} className="px-1 font-mono text-[9px] uppercase text-slate-600">
                  ▸ resumed
                </div>
              )
            if (status === 'failed' || status === 'cancelled')
              return (
                <div key={i} className="px-1">
                  <StatusPill status={status} />
                </div>
              )
            return null
          }
          default:
            return null
        }
      })}
      {ticker.length > 0 && (
        <div className="flex items-center gap-2 px-1 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          <span className="size-1.5 animate-pulse rounded-full bg-accent-400" />
          {ticker.join('  ›  ')}
        </div>
      )}
      {thinking && (
        <details className="animate-rise rounded-md border border-slate-800/60 bg-void-900/40 px-3 py-2">
          <summary className="cursor-pointer select-none font-mono text-[10px] uppercase tracking-widest text-slate-500">
            ∴ model thinking
          </summary>
          <div className="mt-1.5 whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-500">
            {thinking}
          </div>
        </details>
      )}
      {tokens &&
        (() => {
          // swap-at-completion (spec §8.5): raw tokens are the live progress
          // view; when the formatter artifact lands the run settles into its
          // arrangement in place
          const artifact = [...events].reverse().find((e) => e.type === 'answer_ui')
          const chartsEvt = [...events].reverse().find((e) => e.type === 'charts')
          const finished = events.some((e) => e.type === 'done')
          return (
            <div className="animate-rise">
              <AnswerBlock
                markdown={tokens}
                payload={(artifact?.payload as AnswerUiPayload | undefined) ?? null}
                toolCharts={(chartsEvt?.payload.charts as unknown[] | undefined) ?? null}
                streaming={!finished && !artifact}
              />
            </div>
          )
        })()}
      {events.length === 0 && !tokens && (
        <div className="flex items-center gap-2 px-1 py-2 font-mono text-[10px] uppercase tracking-widest text-slate-600">
          <span className="size-1.5 animate-pulse rounded-full bg-accent-400" /> engaging
          orchestrator…
        </div>
      )}
    </div>
  )
}

// ── per-conversation pin (spec §7.5, M40) ────────────────────────
// The composer's target — and its history-summary checkbox — is state of
// the conversation it was set in, never global: switching chats must never
// carry a pin across. The map is keyed by conversation id; '' keys the
// not-yet-created conversation, and the send that creates one carries its
// pin over (a ?target= deep link therefore pins only the chat it opens).

export type ConvPin = { targetId: string | null; includeSummary: boolean }
export const NO_PIN: ConvPin = { targetId: null, includeSummary: false }
type PinMap = Record<string, ConvPin>

export const pinFor = (pins: PinMap, convId: string | null): ConvPin =>
  pins[convId ?? ''] ?? NO_PIN

export const withPin = (pins: PinMap, convId: string | null, patch: Partial<ConvPin>): PinMap => {
  const key = convId ?? ''
  return { ...pins, [key]: { ...(pins[key] ?? NO_PIN), ...patch } }
}

/** First send created the conversation — its draft pin travels with it. */
export const adoptDraftPin = (pins: PinMap, newConvId: string): PinMap => {
  const { '': draft, ...rest } = pins
  return draft ? { ...rest, [newConvId]: draft } : rest
}

/** "+ New conversation" always starts at Orchestrator (auto). */
export const clearDraftPin = (pins: PinMap): PinMap => {
  if (!('' in pins)) return pins
  const rest = { ...pins }
  delete rest['']
  return rest
}

/** §7.5 request body: the pin rides only for its own conversation. */
export const chatBody = (
  text: string,
  convId: string | null,
  pin: ConvPin,
  hasHistory: boolean,
): Record<string, unknown> => {
  const body: Record<string, unknown> = { message: text }
  if (convId) body.conversation_id = convId
  if (pin.targetId) body.target_sub_agent_id = pin.targetId
  if (pin.targetId && pin.includeSummary && hasHistory) body.include_history_summary = true
  return body
}

// ── page ─────────────────────────────────────────────────────────

export function ChatPage() {
  const { data: liveSettings } = useSettings()
  const coverageFlagBelow = liveSettings?.formatter_coverage_flag_threshold
  const { data: conversations = [] } = useConversations()
  const [conversationId, setConversationId] = useState<string | null>(null)
  const { data: detail } = useConversation(conversationId)
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const [liveStatus, setLiveStatus] = useState<string>('running')
  // the queue is bound to the conversation it was queued in — switching
  // chats must never fire it somewhere else
  const [queuedDraft, setQueuedDraft] = useState<{ convId: string; text: string } | null>(null)
  const [message, setMessage] = useState('')
  const queuedHere = queuedDraft !== null && queuedDraft.convId === conversationId
  const [searchParams] = useSearchParams()
  const invalidate = useInvalidate()
  const bottomRef = useRef<HTMLDivElement>(null)
  // direct invocation pin (spec §7.5): messages run against this sub agent,
  // planner bypassed; null = orchestrator (auto). Per-conversation (M40) —
  // includeSummary is only meaningful while pinned AND the conversation
  // already has a completed run (the orchestrator always gets history)
  const [pins, setPins] = useState<Record<string, ConvPin>>({})
  const { targetId, includeSummary } = pinFor(pins, conversationId)
  const setPin = (patch: Partial<ConvPin>) =>
    setPins((p) => withPin(p, conversationId, patch))
  const { data: subAgents = [] } = useSubAgents()
  const exposedAgents = subAgents.filter(
    (a) => a.status === 'active' && a.direct_exposure && !a.deleted_at,
  )
  const agentName = (id: string | null | undefined) =>
    subAgents.find((a) => a.id === id)?.name ?? 'sub agent'

  useEffect(() => {
    // a ?target= deep link (§7.5 surface 3) pins ONLY the conversation it
    // opens — the page mounts on the new-chat draft, so pin that
    const target = searchParams.get('target')
    if (target) setPins((p) => withPin(p, null, { targetId: target }))
  }, [searchParams])

  // re-attach to an in-flight or HITL-paused run when reopening its
  // conversation — the SSE event replay restores the live cards
  useEffect(() => {
    if (liveRunId !== null || !detail) return
    const active = [...detail.runs]
      .reverse()
      .find((r) => r.status === 'running' || r.status === 'paused_hitl')
    if (active) {
      setLiveRunId(active.id)
      setLiveStatus(active.status === 'paused_hitl' ? 'paused_hitl' : 'running')
    }
  }, [detail, liveRunId])

  // entering the queue's conversation restores its draft to the composer;
  // leaving it clears the composer copy (the draft itself is kept)
  useEffect(() => {
    if (!queuedDraft) return
    if (conversationId === queuedDraft.convId) setMessage(queuedDraft.text)
    else setMessage((m) => (m === queuedDraft.text ? '' : m))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  })

  const send = async (textOverride?: string) => {
    const text = (textOverride ?? message).trim()
    if (!text) return
    if (liveRunId) {
      // one message can wait its turn, bound to THIS conversation; it stays
      // editable in the composer while the run works
      if (conversationId) setQueuedDraft({ convId: conversationId, text })
      return
    }
    const body = chatBody(text, conversationId, { targetId, includeSummary }, hasHistory)
    const result = await api.post<{ run_id: string; conversation_id: string }>('/chat', body)
    if (!conversationId) setPins((p) => adoptDraftPin(p, result.conversation_id))
    setConversationId(result.conversation_id)
    setLiveRunId(result.run_id)
    setLiveStatus('running')
    setMessage('')
    invalidate('conversations')
  }

  const stopRun = async () => {
    if (!liveRunId) return
    await api.post(`/runs/${liveRunId}/cancel`).catch(() => {})
  }

  // fire the queued message once ITS conversation's run is truly done —
  // never in another chat, and never on the transient liveRunId gap while
  // switching back (the conversation's own run list is the source of truth)
  useEffect(() => {
    if (!queuedDraft || conversationId !== queuedDraft.convId || liveRunId !== null) return
    if (!detail || detail.id !== conversationId) return
    const stillActive = detail.runs.some(
      (r) => r.status === 'running' || r.status === 'paused_hitl',
    )
    if (stillActive) return // the re-attach effect takes over instead
    const text = queuedDraft.text
    setQueuedDraft(null)
    if (text.trim()) void send(text)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveRunId, queuedDraft, conversationId, detail])

  const liveDone = () => {
    invalidate('conversations', 'runs')
    void api.get(`/conversations/${conversationId}`).then(() => {
      invalidate('conversations')
    })
    setTimeout(() => {
      setLiveRunId(null)
      invalidate('conversations', 'runs', 'hitl-pending')
    }, 400)
  }

  const messages = detail?.messages ?? []
  const hasHistory = detail?.runs.some((r) => r.status === 'completed') ?? false
  const showSummaryOption = targetId !== null && hasHistory

  return (
    <div className="flex h-full">
      <aside className="flex w-60 shrink-0 flex-col border-r border-slate-800/70 bg-void-950/40">
        <div className="p-3">
          <Button
            variant="primary"
            onClick={() => {
              setConversationId(null)
              setLiveRunId(null)
              setPins(clearDraftPin) // a new chat always starts at auto
            }}
          >
            + New conversation
          </Button>
        </div>
        <div className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-4">
          {conversations.map((c) => (
            <button
              key={c.id}
              onClick={() => {
                setConversationId(c.id)
                setLiveRunId(null)
              }}
              className={cx(
                'w-full rounded-md px-3 py-2 text-left transition-colors',
                conversationId === c.id ? 'bg-accent-500/10' : 'hover:bg-slate-900',
              )}
            >
              <div
                className={cx(
                  'line-clamp-1 text-xs font-medium',
                  conversationId === c.id ? 'text-accent-300' : 'text-slate-300',
                )}
              >
                {c.title}
              </div>
              <div className="mt-0.5 font-mono text-[9px] uppercase text-slate-600">
                {c.run_count} runs · {timeAgo(c.updated_at)}
              </div>
            </button>
          ))}
        </div>
      </aside>

      <div className="flex flex-1 flex-col">
        <div className="flex-1 space-y-4 overflow-y-auto px-6 py-5">
          {messages.length === 0 && !liveRunId && (
            <div className="mx-auto mt-24 max-w-md text-center">
              <div className="font-display text-2xl font-bold text-slate-200">
                Concierge<span className="text-accent-400">_</span>
              </div>
              <p className="mt-2 text-sm leading-relaxed text-slate-500">
                One chat over the whole capability registry. The orchestrator plans, routes down
                the ladder — direct → native → custom → ephemeral — and every decision lands in
                the run trace.
              </p>
            </div>
          )}
          {messages.map((m, i) => {
            const run = detail?.runs.find((r) => r.id === m.run_id)
            const direct = run?.orchestrator_mode === 'direct'
            return m.role === 'user' ? (
              <div key={i} className="flex flex-col items-end">
                <div className="max-w-[70%] break-words rounded-lg rounded-br-sm border border-accent-500/30 bg-accent-500/10 px-3.5 py-2 text-sm text-slate-100">
                  {m.content}
                </div>
                {direct && (
                  <div className="mt-0.5 px-1 font-mono text-[9px] uppercase tracking-wider text-accent-400/80">
                    → {agentName(run?.target_sub_agent_id)} · direct
                    {run?.include_history_summary ? ' · +ctx' : ''}
                  </div>
                )}
              </div>
            ) : m.role === 'error' ? (
              // a run that produced no answer still shows why — same look as
              // the live error event, so reloads keep user→response pairing
              <div key={i} className="max-w-[85%]">
                <div className="break-words rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
                  ✕ {m.content}
                </div>
              </div>
            ) : (
              <div key={i} className="max-w-[85%]">
                <AnswerBlock
                  markdown={m.content}
                  payload={m.answer_ui as AnswerUiPayload | null}
                  toolCharts={m.charts}
                  coverageFlagBelow={coverageFlagBelow}
                />
                <div className="mt-1 px-1 font-mono text-[9px] uppercase tracking-wider text-slate-600">
                  <Link to="/runs" className="hover:text-accent-400">
                    run trace ↗
                  </Link>
                </div>
              </div>
            )
          })}
          {liveRunId && (
            <div className="max-w-[85%]">
              <LiveRun runId={liveRunId} onDone={liveDone} onStatus={setLiveStatus} />
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <div className="border-t border-slate-800/70 bg-void-950/60 p-4 backdrop-blur">
          {queuedHere && liveRunId && (
            <div className="mb-1.5 flex items-center gap-2 px-1 font-mono text-[10px] uppercase tracking-widest text-amber-400">
              <span className="animate-blink">⏳</span> 1 message queued in this chat — sends when
              the agent finishes (edit it below)
              <button
                className="text-slate-500 underline-offset-2 hover:text-slate-300 hover:underline"
                onClick={() => setQueuedDraft(null)}
              >
                discard
              </button>
            </div>
          )}
          <div className="mb-1.5 flex items-center gap-2 px-1">
            <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600">
              target
            </span>
            <Select
              value={targetId ?? ''}
              onChange={(e) => setPin({ targetId: e.target.value || null })}
              className="max-w-64 !py-1 !text-xs"
              title="pin messages to a sub agent — direct invocation, planner bypassed"
            >
              <option value="">Orchestrator (auto)</option>
              {exposedAgents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </Select>
            {targetId && (
              <span className="rounded-full border border-accent-500/40 bg-accent-500/10 px-2 py-0.5 font-mono text-[9px] uppercase tracking-widest text-accent-300">
                → {agentName(targetId)} · direct
              </span>
            )}
            {showSummaryOption && (
              <label
                className="flex cursor-pointer items-center gap-1.5 font-mono text-[9px] uppercase tracking-widest text-slate-400"
                title="one summarization call compresses this conversation into the pinned agent's context — off = the agent sees only your message (spec §7.5)"
              >
                <input
                  type="checkbox"
                  checked={includeSummary}
                  onChange={(e) => setPin({ includeSummary: e.target.checked })}
                  className="size-3 accent-[var(--color-accent-400,#2dd4bf)]"
                />
                include chat summary
              </label>
            )}
          </div>
          <div className="flex items-end gap-2">
            <TextArea
              rows={2}
              value={message}
              placeholder={
                liveRunId
                  ? 'Type a message to queue it — it sends when the agent finishes…'
                  : 'Ask the concierge… (Enter to send, Shift+Enter for newline)'
              }
              onChange={(e) => {
                setMessage(e.target.value)
                // queued drafts stay editable — keep the bound copy in sync
                if (queuedHere) setQueuedDraft({ convId: conversationId!, text: e.target.value })
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void send()
                }
              }}
              className="!font-sans !text-sm"
            />
            {liveRunId && liveStatus !== 'completed' ? (
              <div className="flex flex-col gap-1.5">
                <Button variant="danger" onClick={stopRun}>
                  ■ Stop
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => void send()}
                  disabled={!message.trim() || queuedHere}
                >
                  Queue ⏎
                </Button>
              </div>
            ) : (
              <Button variant="primary" onClick={() => void send()} disabled={!message.trim()}>
                Send ⏎
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
