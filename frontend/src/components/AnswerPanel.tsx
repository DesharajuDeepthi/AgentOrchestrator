// Answer rendering (spec §8.5): arrangement is artifact-driven and
// run-time-frozen. Artifact with presentation 'a2ui_first' → structured view
// primary, raw collapsed behind "view raw response". 'raw_first' (or legacy
// artifacts) → raw primary, artifact collapsed behind "show structured
// summary". No artifact → raw only, no toggle at all (the formatter was off
// or failed at run time — there is nothing to fall back to).
import { useState } from 'react'
import { AnswerUiView } from './AnswerUiView'
import { ChartSvg, type ChartSpec } from './ChartSvg'
import { Markdown } from './Markdown'
import { TableBlock, type TableSpec } from './TableBlock'

export interface AnswerUiBlock {
  a2ui?: unknown[]
  chart?: ChartSpec
  tool_chart_ref?: number
  table?: TableSpec
}

export interface AnswerUiPayload {
  a2ui?: unknown[]
  charts?: ChartSpec[]
  presentation?: string
  coverage?: number
  // ordered render blocks (spec §8.5): present when the formatter placed a
  // chart mid-flow — segments and charts render in sequence
  blocks?: AnswerUiBlock[]
}

const RAW_BUBBLE =
  'break-words rounded-lg rounded-bl-sm border border-slate-800 bg-void-900/70 px-3.5 py-2 text-sm leading-relaxed text-slate-200'
const STRUCTURED_SHELL =
  'rounded-lg rounded-bl-sm border border-slate-800 bg-void-900/70 px-3.5 py-2.5 space-y-2'

function CoverageBadge({ coverage, flagBelow }: { coverage?: number; flagBelow?: number }) {
  if (coverage == null) return null
  const low = flagBelow != null && coverage < flagBelow
  return (
    <span
      title="content coverage: numbers/URLs/code spans of the raw answer retained by the structured view"
      className={`ml-2 font-mono text-[9px] uppercase tracking-wider ${
        low ? 'text-amber-400' : 'text-slate-600'
      }`}
    >
      coverage {coverage}%{low ? ' ⚠' : ''}
    </span>
  )
}

function ToolCharts({ charts }: { charts: ChartSpec[] }) {
  if (!charts.length) return null
  return (
    <div className="mt-1.5 space-y-2">
      {charts.map((c, i) => (
        <ChartSvg key={i} spec={c} />
      ))}
    </div>
  )
}

/** Tool-chart indices a blocks payload places inline (valid refs only). */
function placedRefs(payload: AnswerUiPayload | null | undefined, toolCount: number): Set<number> {
  const placed = new Set<number>()
  for (const b of payload?.blocks ?? []) {
    if (b.tool_chart_ref != null && b.tool_chart_ref >= 0 && b.tool_chart_ref < toolCount)
      placed.add(b.tool_chart_ref)
  }
  return placed
}

/** The structured document: ordered blocks when the formatter placed charts
 * mid-flow, else the legacy layout (charts hoisted above the a2ui tree). */
function StructuredDoc({
  payload,
  toolCharts,
}: {
  payload: AnswerUiPayload
  toolCharts: ChartSpec[]
}) {
  if (payload.blocks?.length) {
    return (
      <div className={STRUCTURED_SHELL}>
        <div className="font-mono text-[9px] uppercase tracking-[0.2em] text-slate-500">
          answer panel · structured view
        </div>
        {payload.blocks.map((b, i) => {
          if (b.a2ui) return <AnswerUiView key={i} bare messages={b.a2ui} />
          if (b.chart) return <ChartSvg key={i} spec={b.chart} />
          if (b.table) return <TableBlock key={i} spec={b.table} />
          if (b.tool_chart_ref != null && toolCharts[b.tool_chart_ref])
            return <ChartSvg key={i} spec={toolCharts[b.tool_chart_ref]} />
          return null
        })}
      </div>
    )
  }
  return (
    <div className={STRUCTURED_SHELL}>
      {payload.charts?.map((c, i) => <ChartSvg key={i} spec={c} />)}
      {payload.a2ui && <AnswerUiView messages={payload.a2ui} />}
    </div>
  )
}

export function AnswerBlock({
  markdown,
  payload,
  toolCharts,
  coverageFlagBelow,
  streaming = false,
}: {
  markdown: string
  payload: AnswerUiPayload | null | undefined
  toolCharts?: unknown[] | null
  coverageFlagBelow?: number
  streaming?: boolean
}) {
  const [showAlt, setShowAlt] = useState(false)
  const charts = (toolCharts ?? []) as ChartSpec[]
  const hasArtifact = !!payload && (!!payload.a2ui || !!payload.charts?.length)
  const a2uiFirst = hasArtifact && payload?.presentation === 'a2ui_first'

  const rawView = (
    <div className={RAW_BUBBLE}>
      <Markdown text={markdown} />
      {streaming && <span className="animate-blink text-accent-400">▮</span>}
    </div>
  )
  const structuredView = hasArtifact ? (
    <StructuredDoc payload={payload!} toolCharts={charts} />
  ) : null

  if (!hasArtifact) {
    // formatter off or failed at run time: raw renders directly — no toggle
    return (
      <div>
        {rawView}
        <ToolCharts charts={charts} />
      </div>
    )
  }

  if (a2uiFirst) {
    // tool charts the document places inline don't repeat in the slot below
    const placed = placedRefs(payload, charts.length)
    const unplaced = charts.filter((_, i) => !placed.has(i))
    return (
      <div>
        {structuredView}
        <ToolCharts charts={unplaced} />
        <div className="mt-1.5">
          <button
            onClick={() => setShowAlt(!showAlt)}
            className="font-mono text-[10px] tracking-wider text-slate-500 uppercase transition-colors hover:text-accent-400"
          >
            {showAlt ? '▾ hide raw response' : '▸ view raw response'}
          </button>
          <CoverageBadge coverage={payload?.coverage} flagBelow={coverageFlagBelow} />
          {showAlt && <div className="animate-rise mt-1.5">{rawView}</div>}
        </div>
      </div>
    )
  }

  return (
    <div>
      {rawView}
      <ToolCharts charts={charts} />
      <div className="mt-1.5">
        <button
          onClick={() => setShowAlt(!showAlt)}
          className="font-mono text-[10px] tracking-wider text-slate-500 uppercase transition-colors hover:text-accent-400"
        >
          {showAlt ? '▾ hide structured summary' : '▸ show structured summary'}
        </button>
        <CoverageBadge coverage={payload?.coverage} flagBelow={coverageFlagBelow} />
        {showAlt && <div className="animate-rise mt-1.5">{structuredView}</div>}
      </div>
    </div>
  )
}

/** Trace view (Runs page): the audit surface — everything expanded. */
export function AnswerTrace({
  markdown,
  payload,
  toolCharts,
}: {
  markdown: string | null
  payload: AnswerUiPayload | null | undefined
  toolCharts?: unknown[] | null
}) {
  const charts = (toolCharts ?? []) as ChartSpec[]
  const hasArtifact = !!payload && (!!payload.a2ui || !!payload.charts?.length)
  return (
    <div className="space-y-2">
      {markdown && (
        <div className={RAW_BUBBLE}>
          <Markdown text={markdown} />
        </div>
      )}
      <ToolCharts charts={charts} />
      {hasArtifact && (
        <div>
          <div className="mb-1 font-mono text-[9px] uppercase tracking-widest text-slate-500">
            structured artifact
            <CoverageBadge coverage={payload?.coverage} />
          </div>
          <StructuredDoc payload={payload!} toolCharts={charts} />
        </div>
      )}
    </div>
  )
}
