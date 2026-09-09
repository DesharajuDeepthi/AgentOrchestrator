// Themed pure-SVG charts (spec §7.1 chart component): data only, no markup
// surface, colors from the theme's accent ramp so all four themes follow.
export interface ChartSpec {
  kind:
    | 'bar'
    | 'hbar'
    | 'stacked_bar'
    | 'stacked_bar_100'
    | 'line'
    | 'area'
    | 'stacked_area'
    | 'pie'
    | 'donut'
    | 'histogram'
    | 'funnel'
    | 'waterfall'
    | 'lollipop'
    | 'gauge'
    | 'sparkline'
    | 'scatter'
    | 'bubble'
    | 'candlestick'
    | 'boxplot'
    | 'gantt'
    | 'combo'
  title: string
  labels: string[]
  series: { name: string; values: number[]; points?: number[][]; render?: 'bar' | 'line' }[]
  ranges?: string[][]
}

const W = 460
const H = 200
const PAD = { top: 14, right: 12, bottom: 34, left: 40 }
const SERIES_VARS = [
  'var(--color-accent-400)',
  'var(--color-amber-400)',
  'var(--color-slate-500)',
  'var(--color-accent-600)',
]

function niceMax(v: number): number {
  if (v <= 0) return 1
  const mag = 10 ** Math.floor(Math.log10(v))
  const n = v / mag
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag
}

function Axes({ max }: { max: number }) {
  const y0 = H - PAD.bottom
  const ticks = [0, 0.5, 1]
  return (
    <g className="text-slate-600">
      <line x1={PAD.left} y1={y0} x2={W - PAD.right} y2={y0} stroke="currentColor" />
      <line x1={PAD.left} y1={PAD.top} x2={PAD.left} y2={y0} stroke="currentColor" />
      {ticks.map((t) => (
        <text
          key={t}
          x={PAD.left - 5}
          y={y0 - t * (y0 - PAD.top) + 3}
          textAnchor="end"
          fontSize="9"
          fill="var(--color-slate-500)"
        >
          {Math.round(max * t * 100) / 100}
        </text>
      ))}
    </g>
  )
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}/

/** Dense label sets thin to ~8 ticks; all-ISO-date labels drop the year
 * after the first tick so time series stay legible. */
function displayLabels(labels: string[]): { text: string; index: number }[] {
  const step = Math.max(1, Math.ceil(labels.length / 8))
  const allDates = labels.length > 1 && labels.every((l) => ISO_DATE.test(l))
  const out: { text: string; index: number }[] = []
  for (let i = 0; i < labels.length; i += step) {
    let text = labels[i]
    if (allDates && i > 0) text = text.slice(5, 10)
    else if (text.length > 12) text = text.slice(0, 11) + '…'
    out.push({ text, index: i })
  }
  return out
}

function XLabels({ labels }: { labels: string[] }) {
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  return (
    <g>
      {displayLabels(labels).map(({ text, index }) => (
        <text
          key={index}
          x={PAD.left + span * ((index + 0.5) / labels.length)}
          y={y0 + 13}
          textAnchor="middle"
          fontSize="9"
          fill="var(--color-slate-400)"
        >
          {text}
        </text>
      ))}
    </g>
  )
}

function BarChart({ spec, gapless = false }: { spec: ChartSpec; gapless?: boolean }) {
  const series = gapless ? spec.series.slice(0, 1) : spec.series
  const max = niceMax(Math.max(...series.flatMap((s) => s.values)))
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const slot = span / spec.labels.length
  const barW = gapless ? slot : Math.min(34, (slot * 0.7) / series.length)
  return (
    <>
      <Axes max={max} />
      <XLabels labels={spec.labels} />
      {series.map((s, si) =>
        s.values.map((v, i) => {
          const h = ((y0 - PAD.top) * v) / max
          const x = gapless
            ? PAD.left + slot * i
            : PAD.left + slot * i + slot / 2 - (barW * series.length) / 2 + si * barW
          return (
            <rect
              key={`${si}-${i}`}
              x={x}
              y={y0 - h}
              width={gapless ? barW - 1 : barW - 2}
              height={h}
              rx={gapless ? 0 : 2}
              fill={SERIES_VARS[si % SERIES_VARS.length]}
              opacity={0.9}
            />
          )
        }),
      )}
    </>
  )
}

function StackedBarChart({ spec }: { spec: ChartSpec }) {
  const totals = spec.labels.map((_, i) =>
    spec.series.reduce((a, s) => a + (s.values[i] ?? 0), 0),
  )
  const max = niceMax(Math.max(...totals))
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const slot = span / spec.labels.length
  const barW = Math.min(40, slot * 0.6)
  return (
    <>
      <Axes max={max} />
      <XLabels labels={spec.labels} />
      {spec.labels.map((_, i) => {
        let acc = 0
        return spec.series.map((s, si) => {
          const v = s.values[i] ?? 0
          const h = ((y0 - PAD.top) * v) / max
          const y = y0 - ((y0 - PAD.top) * acc) / max - h
          acc += v
          return (
            <rect
              key={`${si}-${i}`}
              x={PAD.left + slot * i + (slot - barW) / 2}
              y={y}
              width={barW}
              height={h}
              fill={SERIES_VARS[si % SERIES_VARS.length]}
              opacity={0.9}
            />
          )
        })
      })}
    </>
  )
}

function HBarChart({ spec }: { spec: ChartSpec }) {
  const values = spec.series[0]?.values ?? []
  const max = niceMax(Math.max(...values))
  const left = 110
  const span = W - left - PAD.right
  const slot = (H - PAD.top - 10) / spec.labels.length
  const barH = Math.min(20, slot * 0.65)
  return (
    <>
      <line
        x1={left}
        y1={PAD.top}
        x2={left}
        y2={H - 10}
        stroke="currentColor"
        className="text-slate-600"
      />
      {spec.labels.map((l, i) => {
        const w = (span * (values[i] ?? 0)) / max
        const y = PAD.top + slot * i + (slot - barH) / 2
        return (
          <g key={i}>
            <text
              x={left - 6}
              y={y + barH / 2 + 3}
              textAnchor="end"
              fontSize="9"
              fill="var(--color-slate-400)"
            >
              {l.length > 16 ? l.slice(0, 15) + '…' : l}
            </text>
            <rect x={left} y={y} width={w} height={barH} rx={2} fill={SERIES_VARS[0]} opacity={0.9} />
            <text x={left + w + 5} y={y + barH / 2 + 3} fontSize="9" fill="var(--color-slate-500)">
              {values[i]}
            </text>
          </g>
        )
      })}
    </>
  )
}

function LineChart({ spec, filled = false }: { spec: ChartSpec; filled?: boolean }) {
  const max = niceMax(Math.max(...spec.series.flatMap((s) => s.values)))
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const px = (i: number) => PAD.left + span * ((i + 0.5) / spec.labels.length)
  const py = (v: number) => y0 - ((y0 - PAD.top) * v) / max
  const pt = (i: number, v: number) => `${px(i)},${py(v)}`
  const dense = spec.labels.length > 20
  return (
    <>
      <Axes max={max} />
      <XLabels labels={spec.labels} />
      {spec.series.map((s, si) => (
        <g key={si}>
          {filled && s.values.length > 1 && (
            <polygon
              points={[
                `${px(0)},${y0}`,
                ...s.values.map((v, i) => pt(i, v)),
                `${px(s.values.length - 1)},${y0}`,
              ].join(' ')}
              fill={SERIES_VARS[si % SERIES_VARS.length]}
              opacity={0.18}
            />
          )}
          <polyline
            points={s.values.map((v, i) => pt(i, v)).join(' ')}
            fill="none"
            stroke={SERIES_VARS[si % SERIES_VARS.length]}
            strokeWidth={2}
          />
          {!dense &&
            s.values.map((v, i) => (
              <circle
                key={i}
                cx={px(i)}
                cy={py(v)}
                r={3}
                fill={SERIES_VARS[si % SERIES_VARS.length]}
              />
            ))}
        </g>
      ))}
    </>
  )
}

function PieChart({ spec, innerRatio = 0 }: { spec: ChartSpec; innerRatio?: number }) {
  const values = spec.series[0]?.values ?? []
  const total = values.reduce((a, b) => a + b, 0) || 1
  const cx = W / 2 - 80
  const cy = H / 2
  const r = 70
  const ri = r * innerRatio
  let angle = -Math.PI / 2
  const slices = values.map((v, i) => {
    const sweep = (v / total) * Math.PI * 2
    const a1 = angle
    angle += sweep
    const a2 = angle
    const large = sweep > Math.PI ? 1 : 0
    const x1 = cx + r * Math.cos(a1)
    const y1 = cy + r * Math.sin(a1)
    const x2 = cx + r * Math.cos(a2)
    const y2 = cy + r * Math.sin(a2)
    const d =
      ri > 0
        ? `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} ` +
          `L ${cx + ri * Math.cos(a2)} ${cy + ri * Math.sin(a2)} ` +
          `A ${ri} ${ri} 0 ${large} 0 ${cx + ri * Math.cos(a1)} ${cy + ri * Math.sin(a1)} Z`
        : `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`
    return (
      <path
        key={i}
        d={d}
        fill={SERIES_VARS[i % SERIES_VARS.length]}
        opacity={0.75 + 0.25 * ((i % 3) / 2)}
        stroke="var(--color-void-950)"
        strokeWidth={1}
      />
    )
  })
  return (
    <>
      {slices}
      <g>
        {spec.labels.map((l, i) => (
          <g key={i} transform={`translate(${W / 2 + 30}, ${PAD.top + 8 + i * 16})`}>
            <rect width={9} height={9} rx={2} fill={SERIES_VARS[i % SERIES_VARS.length]} />
            <text x={14} y={8} fontSize="10" fill="var(--color-slate-300)">
              {l} — {Math.round(((values[i] ?? 0) / total) * 100)}%
            </text>
          </g>
        ))}
      </g>
    </>
  )
}

function StackedBar100({ spec }: { spec: ChartSpec }) {
  const pct: ChartSpec = {
    ...spec,
    series: spec.series.map((s) => ({
      ...s,
      values: s.values.map((v, i) => {
        const total = spec.series.reduce((a, x) => a + (x.values[i] ?? 0), 0) || 1
        return (100 * v) / total
      }),
    })),
  }
  return <StackedBarChart spec={pct} />
}

function StackedAreaChart({ spec }: { spec: ChartSpec }) {
  const max = niceMax(
    Math.max(...spec.labels.map((_, i) => spec.series.reduce((a, s) => a + (s.values[i] ?? 0), 0))),
  )
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const px = (i: number) => PAD.left + span * ((i + 0.5) / spec.labels.length)
  const py = (v: number) => y0 - ((y0 - PAD.top) * v) / max
  const cum: number[][] = []
  let prev = spec.labels.map(() => 0)
  for (const s of spec.series) {
    prev = prev.map((v, i) => v + (s.values[i] ?? 0))
    cum.push([...prev])
  }
  return (
    <>
      <Axes max={max} />
      <XLabels labels={spec.labels} />
      {spec.series.map((_, si) => {
        const top = cum[si]
        const bottom = si === 0 ? spec.labels.map(() => 0) : cum[si - 1]
        const pts = [
          ...top.map((v, i) => `${px(i)},${py(v)}`),
          ...bottom.map((_, i) => `${px(bottom.length - 1 - i)},${py(bottom[bottom.length - 1 - i])}`),
        ].join(' ')
        return (
          <polygon
            key={si}
            points={pts}
            fill={SERIES_VARS[si % SERIES_VARS.length]}
            opacity={0.55}
            stroke={SERIES_VARS[si % SERIES_VARS.length]}
            strokeWidth={1}
          />
        )
      })}
    </>
  )
}

function LollipopChart({ spec }: { spec: ChartSpec }) {
  const values = spec.series[0]?.values ?? []
  const max = niceMax(Math.max(...values))
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const slot = span / spec.labels.length
  return (
    <>
      <Axes max={max} />
      <XLabels labels={spec.labels} />
      {values.map((v, i) => {
        const x = PAD.left + slot * i + slot / 2
        const y = y0 - ((y0 - PAD.top) * v) / max
        return (
          <g key={i}>
            <line x1={x} y1={y0} x2={x} y2={y} stroke={SERIES_VARS[0]} strokeWidth={2} opacity={0.6} />
            <circle cx={x} cy={y} r={5} fill={SERIES_VARS[0]} />
          </g>
        )
      })}
    </>
  )
}

function FunnelChart({ spec }: { spec: ChartSpec }) {
  const values = spec.series[0]?.values ?? []
  const first = values[0] || 1
  const rowH = Math.min(26, (H - 20) / values.length)
  const cx = W / 2 - 40
  const maxW = 260
  return (
    <>
      {values.map((v, i) => {
        const w = Math.max(6, (maxW * v) / first)
        const y = 12 + i * (rowH + 4)
        return (
          <g key={i}>
            <rect
              x={cx - w / 2}
              y={y}
              width={w}
              height={rowH}
              rx={3}
              fill={SERIES_VARS[i % SERIES_VARS.length]}
              opacity={0.85}
            />
            <text x={cx + maxW / 2 + 14} y={y + rowH / 2 + 3} fontSize="10" fill="var(--color-slate-300)">
              {spec.labels[i]} — {v} ({Math.round((100 * v) / first)}%)
            </text>
          </g>
        )
      })}
    </>
  )
}

function WaterfallChart({ spec }: { spec: ChartSpec }) {
  const deltas = spec.series[0]?.values ?? []
  const cums = [0]
  for (const d of deltas) cums.push(cums[cums.length - 1] + d)
  const lo = Math.min(0, ...cums)
  const hi = Math.max(...cums, 1)
  const max = niceMax(hi - lo)
  const y0 = H - PAD.bottom
  const scale = (y0 - PAD.top) / max
  const yFor = (v: number) => y0 - (v - lo) * scale
  const span = W - PAD.left - PAD.right
  const slot = span / deltas.length
  const barW = Math.min(38, slot * 0.6)
  return (
    <>
      <XLabels labels={spec.labels} />
      <line x1={PAD.left} y1={yFor(0)} x2={W - PAD.right} y2={yFor(0)} stroke="currentColor" className="text-slate-600" />
      {deltas.map((d, i) => {
        const start = cums[i]
        const end = cums[i + 1]
        const x = PAD.left + slot * i + (slot - barW) / 2
        const yTop = yFor(Math.max(start, end))
        const h = Math.max(2, Math.abs(end - start) * scale)
        return (
          <g key={i}>
            <rect
              x={x}
              y={yTop}
              width={barW}
              height={h}
              rx={2}
              fill={d >= 0 ? SERIES_VARS[0] : 'var(--color-amber-400)'}
              opacity={0.9}
            />
            {i < deltas.length - 1 && (
              <line
                x1={x + barW}
                y1={yFor(end)}
                x2={PAD.left + slot * (i + 1) + (slot - barW) / 2}
                y2={yFor(end)}
                stroke="var(--color-slate-500)"
                strokeDasharray="3 2"
              />
            )}
          </g>
        )
      })}
    </>
  )
}

function GaugeChart({ spec }: { spec: ChartSpec }) {
  const [value = 0, max = 1] = spec.series[0]?.values ?? []
  const frac = Math.max(0, Math.min(1, value / (max || 1)))
  const cx = W / 2
  const cy = H - 50
  const r = 90
  const arc = (from: number, to: number) => {
    const a1 = Math.PI * (1 - from)
    const a2 = Math.PI * (1 - to)
    const large = to - from > 0.5 ? 1 : 0
    return `M ${cx + r * Math.cos(a1)} ${cy - r * Math.sin(a1)} A ${r} ${r} 0 ${large} 1 ${cx + r * Math.cos(a2)} ${cy - r * Math.sin(a2)}`
  }
  return (
    <>
      <path d={arc(0, 1)} fill="none" stroke="var(--color-slate-500)" strokeWidth={14} opacity={0.35} strokeLinecap="round" />
      <path d={arc(0, frac)} fill="none" stroke={SERIES_VARS[0]} strokeWidth={14} strokeLinecap="round" />
      <text x={cx} y={cy - 14} textAnchor="middle" fontSize="26" fontWeight="600" fill="var(--color-slate-200)">
        {value}
      </text>
      <text x={cx} y={cy + 6} textAnchor="middle" fontSize="11" fill="var(--color-slate-500)">
        of {max}
        {spec.labels[0] ? ` — ${spec.labels[0]}` : ''}
      </text>
    </>
  )
}

function SparklineChart({ spec }: { spec: ChartSpec }) {
  const SH = 90
  const values = spec.series[0]?.values ?? []
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const range = hi - lo || 1
  const px = (i: number) => 8 + (W - 60) * (i / Math.max(1, values.length - 1))
  const py = (v: number) => SH - 18 - (SH - 36) * ((v - lo) / range)
  return (
    <>
      <polyline
        points={values.map((v, i) => `${px(i)},${py(v)}`).join(' ')}
        fill="none"
        stroke={SERIES_VARS[0]}
        strokeWidth={2.5}
      />
      <circle cx={px(values.length - 1)} cy={py(values[values.length - 1] ?? 0)} r={4} fill={SERIES_VARS[0]} />
      <text x={W - 8} y={py(values[values.length - 1] ?? 0) - 8} textAnchor="end" fontSize="11" fill="var(--color-slate-300)">
        {values[values.length - 1]}
      </text>
    </>
  )
}

function ScatterChart({ spec, sized = false }: { spec: ChartSpec; sized?: boolean }) {
  const pts = spec.series.flatMap((s) => s.points ?? [])
  if (!pts.length) return null
  const xs = pts.map((p) => p[0])
  const ys = pts.map((p) => p[1])
  const xLo = Math.min(...xs)
  const xHi = Math.max(...xs)
  const yHi = niceMax(Math.max(...ys))
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const px = (x: number) => PAD.left + span * ((x - xLo) / (xHi - xLo || 1))
  const py = (y: number) => y0 - (y0 - PAD.top) * (y / yHi)
  const maxSize = sized ? Math.max(...pts.map((p) => p[2] ?? 1)) : 1
  return (
    <>
      <Axes max={yHi} />
      <text x={PAD.left} y={y0 + 13} fontSize="9" fill="var(--color-slate-400)">
        {xLo}
      </text>
      <text x={W - PAD.right} y={y0 + 13} textAnchor="end" fontSize="9" fill="var(--color-slate-400)">
        {xHi}
      </text>
      {spec.series.map((s, si) =>
        (s.points ?? []).map((p, i) => (
          <circle
            key={`${si}-${i}`}
            cx={px(p[0])}
            cy={py(p[1])}
            r={sized ? 4 + 10 * Math.sqrt((p[2] ?? 1) / maxSize) : 4}
            fill={SERIES_VARS[si % SERIES_VARS.length]}
            opacity={sized ? 0.55 : 0.8}
          />
        )),
      )}
    </>
  )
}

function CandlestickChart({ spec }: { spec: ChartSpec }) {
  const by = (n: string) => spec.series.find((s) => s.name.toLowerCase() === n)?.values ?? []
  const open = by('open')
  const high = by('high')
  const low = by('low')
  const close = by('close')
  // price charts scale to the actual range (with padding), not to zero —
  // niceMax would squash a 229–241 week into the bottom of a 0–500 axis
  const rawHi = Math.max(...high)
  const rawLo = Math.min(...low)
  const pad = (rawHi - rawLo) * 0.08 || 1
  const hi = rawHi + pad
  const lo = rawLo - pad
  const y0 = H - PAD.bottom
  const scale = (y0 - PAD.top) / (hi - lo || 1)
  const yFor = (v: number) => y0 - (v - lo) * scale
  const span = W - PAD.left - PAD.right
  const slot = span / spec.labels.length
  const bw = Math.min(14, slot * 0.5)
  return (
    <>
      <XLabels labels={spec.labels} />
      <line x1={PAD.left} y1={PAD.top} x2={PAD.left} y2={y0} stroke="currentColor" className="text-slate-600" />
      <text x={PAD.left - 5} y={yFor(rawHi) + 3} textAnchor="end" fontSize="9" fill="var(--color-slate-500)">{rawHi}</text>
      <text x={PAD.left - 5} y={yFor(rawLo) + 3} textAnchor="end" fontSize="9" fill="var(--color-slate-500)">{rawLo}</text>
      {spec.labels.map((_, i) => {
        const x = PAD.left + slot * i + slot / 2
        const up = (close[i] ?? 0) >= (open[i] ?? 0)
        const color = up ? SERIES_VARS[0] : 'var(--color-amber-400)'
        const bodyTop = yFor(Math.max(open[i] ?? 0, close[i] ?? 0))
        const bodyH = Math.max(2, Math.abs((close[i] ?? 0) - (open[i] ?? 0)) * scale)
        return (
          <g key={i}>
            <line x1={x} y1={yFor(high[i] ?? 0)} x2={x} y2={yFor(low[i] ?? 0)} stroke={color} strokeWidth={1.5} />
            <rect x={x - bw / 2} y={bodyTop} width={bw} height={bodyH} fill={color} opacity={0.9} rx={1} />
          </g>
        )
      })}
    </>
  )
}

function BoxplotChart({ spec }: { spec: ChartSpec }) {
  const by = (n: string) => spec.series.find((s) => s.name.toLowerCase() === n)?.values ?? []
  const mins = by('min')
  const q1 = by('q1')
  const med = by('median')
  const q3 = by('q3')
  const maxs = by('max')
  const hi = niceMax(Math.max(...maxs))
  const lo = Math.min(...mins)
  const y0 = H - PAD.bottom
  const scale = (y0 - PAD.top) / (hi - lo || 1)
  const yFor = (v: number) => y0 - (v - lo) * scale
  const span = W - PAD.left - PAD.right
  const slot = span / spec.labels.length
  const bw = Math.min(30, slot * 0.5)
  return (
    <>
      <XLabels labels={spec.labels} />
      <line x1={PAD.left} y1={PAD.top} x2={PAD.left} y2={y0} stroke="currentColor" className="text-slate-600" />
      {spec.labels.map((_, i) => {
        const x = PAD.left + slot * i + slot / 2
        return (
          <g key={i} stroke={SERIES_VARS[0]}>
            <line x1={x} y1={yFor(mins[i] ?? 0)} x2={x} y2={yFor(maxs[i] ?? 0)} strokeWidth={1.5} />
            <rect
              x={x - bw / 2}
              y={yFor(q3[i] ?? 0)}
              width={bw}
              height={Math.max(2, ((q3[i] ?? 0) - (q1[i] ?? 0)) * scale)}
              fill={SERIES_VARS[0]}
              opacity={0.35}
              strokeWidth={1.5}
            />
            <line x1={x - bw / 2} y1={yFor(med[i] ?? 0)} x2={x + bw / 2} y2={yFor(med[i] ?? 0)} strokeWidth={2.5} />
          </g>
        )
      })}
    </>
  )
}

function GanttChart({ spec }: { spec: ChartSpec }) {
  const ranges = spec.ranges ?? []
  const times = ranges.flat().map((d) => Date.parse(d)).filter((t) => !Number.isNaN(t))
  if (!times.length) return null
  const t0 = Math.min(...times)
  const t1 = Math.max(...times)
  const left = 120
  const span = W - left - PAD.right
  const slot = (H - 30) / spec.labels.length
  const barH = Math.min(16, slot * 0.6)
  const xFor = (t: number) => left + (span * (t - t0)) / (t1 - t0 || 1)
  const fmt = (t: number) => new Date(t).toISOString().slice(5, 10)
  return (
    <>
      {spec.labels.map((l, i) => {
        const s = Date.parse(ranges[i]?.[0] ?? '')
        const e = Date.parse(ranges[i]?.[1] ?? '')
        if (Number.isNaN(s) || Number.isNaN(e)) return null
        const y = 10 + slot * i + (slot - barH) / 2
        return (
          <g key={i}>
            <text x={left - 6} y={y + barH / 2 + 3} textAnchor="end" fontSize="9" fill="var(--color-slate-400)">
              {l.length > 18 ? l.slice(0, 17) + '…' : l}
            </text>
            <rect
              x={xFor(s)}
              y={y}
              width={Math.max(4, xFor(e) - xFor(s))}
              height={barH}
              rx={3}
              fill={SERIES_VARS[i % SERIES_VARS.length]}
              opacity={0.85}
            />
          </g>
        )
      })}
      <text x={left} y={H - 4} fontSize="9" fill="var(--color-slate-500)">{fmt(t0)}</text>
      <text x={W - PAD.right} y={H - 4} textAnchor="end" fontSize="9" fill="var(--color-slate-500)">{fmt(t1)}</text>
    </>
  )
}

function ComboChart({ spec }: { spec: ChartSpec }) {
  const bars = spec.series.filter((s) => (s.render ?? 'bar') === 'bar')
  const lines = spec.series.filter((s) => s.render === 'line')
  const max = niceMax(Math.max(...spec.series.flatMap((s) => s.values)))
  const y0 = H - PAD.bottom
  const span = W - PAD.left - PAD.right
  const slot = span / spec.labels.length
  const barW = Math.min(30, (slot * 0.65) / Math.max(1, bars.length))
  const px = (i: number) => PAD.left + span * ((i + 0.5) / spec.labels.length)
  const py = (v: number) => y0 - ((y0 - PAD.top) * v) / max
  return (
    <>
      <Axes max={max} />
      <XLabels labels={spec.labels} />
      {bars.map((s, si) =>
        s.values.map((v, i) => (
          <rect
            key={`b${si}-${i}`}
            x={PAD.left + slot * i + slot / 2 - (barW * bars.length) / 2 + si * barW}
            y={py(v)}
            width={barW - 2}
            height={y0 - py(v)}
            rx={2}
            fill={SERIES_VARS[si % SERIES_VARS.length]}
            opacity={0.85}
          />
        )),
      )}
      {lines.map((s, si) => (
        <polyline
          key={`l${si}`}
          points={s.values.map((v, i) => `${px(i)},${py(v)}`).join(' ')}
          fill="none"
          stroke={SERIES_VARS[(bars.length + si) % SERIES_VARS.length]}
          strokeWidth={2.5}
        />
      ))}
    </>
  )
}

// kinds whose legend is built into the drawing (or meaningless as series)
const ROUND_KINDS = new Set(['pie', 'donut', 'gauge', 'funnel', 'sparkline', 'gantt', 'candlestick', 'boxplot'])

const NO_LABEL_KINDS = new Set(['scatter', 'bubble', 'sparkline'])

export function ChartSvg({ spec }: { spec: ChartSpec }) {
  if (!spec.labels.length && !NO_LABEL_KINDS.has(spec.kind)) return null
  if (!spec.series.length && spec.kind !== 'gantt') return null
  return (
    <div className="rounded-md border border-slate-800 bg-void-900/50 p-2.5">
      {spec.title && (
        <div className="mb-1 px-1 font-mono text-[10px] tracking-wider text-slate-400 uppercase">
          {spec.title}
        </div>
      )}
      <svg
        viewBox={`0 0 ${W} ${spec.kind === 'sparkline' ? 90 : H}`}
        className="w-full"
        style={spec.kind === 'sparkline' ? { maxWidth: 220 } : undefined}
        role="img"
        aria-label={spec.title}
      >
        {spec.kind === 'bar' && <BarChart spec={spec} />}
        {spec.kind === 'histogram' && <BarChart spec={spec} gapless />}
        {spec.kind === 'stacked_bar' && <StackedBarChart spec={spec} />}
        {spec.kind === 'stacked_bar_100' && <StackedBar100 spec={spec} />}
        {spec.kind === 'hbar' && <HBarChart spec={spec} />}
        {spec.kind === 'line' && <LineChart spec={spec} />}
        {spec.kind === 'area' && <LineChart spec={spec} filled />}
        {spec.kind === 'stacked_area' && <StackedAreaChart spec={spec} />}
        {spec.kind === 'pie' && <PieChart spec={spec} />}
        {spec.kind === 'donut' && <PieChart spec={spec} innerRatio={0.55} />}
        {spec.kind === 'funnel' && <FunnelChart spec={spec} />}
        {spec.kind === 'waterfall' && <WaterfallChart spec={spec} />}
        {spec.kind === 'lollipop' && <LollipopChart spec={spec} />}
        {spec.kind === 'gauge' && <GaugeChart spec={spec} />}
        {spec.kind === 'sparkline' && <SparklineChart spec={spec} />}
        {spec.kind === 'scatter' && <ScatterChart spec={spec} />}
        {spec.kind === 'bubble' && <ScatterChart spec={spec} sized />}
        {spec.kind === 'candlestick' && <CandlestickChart spec={spec} />}
        {spec.kind === 'boxplot' && <BoxplotChart spec={spec} />}
        {spec.kind === 'gantt' && <GanttChart spec={spec} />}
        {spec.kind === 'combo' && <ComboChart spec={spec} />}
      </svg>
      {!ROUND_KINDS.has(spec.kind) && spec.series.some((s) => s.name) && (
        <div className="mt-1 flex flex-wrap gap-3 px-1">
          {spec.series.map((s, i) => (
            <span key={i} className="flex items-center gap-1.5 text-[10px] text-slate-400">
              <span
                className="inline-block size-2 rounded-sm"
                style={{ background: SERIES_VARS[i % SERIES_VARS.length] }}
              />
              {s.name || `series ${i + 1}`}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
