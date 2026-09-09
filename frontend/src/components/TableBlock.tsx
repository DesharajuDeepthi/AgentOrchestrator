// Native themed table (spec §8.5): real table chrome for `table` components
// in the structured document — header row, zebra striping, numeric columns
// right-aligned in monospace, horizontal scroll for wide tables.
export interface TableSpec {
  columns: string[]
  rows: string[][]
  // per-column color scale over numeric cells (heatmap mode)
  heat?: boolean
}

const NUMERIC = /^[\s$€£]?-?\d[\d,]*(\.\d+)?\s?(%|ms|s|units?|x)?$/i
const num = (v: string) => parseFloat(String(v).replace(/[^\d.-]/g, ''))

export function TableBlock({ spec }: { spec: TableSpec }) {
  if (!spec.rows.length) return null
  const cols = spec.columns.length
    ? spec.columns
    : Array.from({ length: spec.rows[0]?.length ?? 0 }, () => '')
  const numeric = cols.map((_, ci) =>
    spec.rows.every((r) => r[ci] == null || r[ci] === '' || NUMERIC.test(String(r[ci]).trim())),
  )
  // heat: normalize each numeric column independently (deterministic display
  // math only — mixed-scale columns stay comparable within themselves)
  const heatFor = (ci: number, v: string): string | undefined => {
    if (!spec.heat || !numeric[ci]) return undefined
    const vals = spec.rows.map((r) => num(r[ci] ?? '')).filter((x) => !Number.isNaN(x))
    if (vals.length < 2) return undefined
    const lo = Math.min(...vals)
    const hi = Math.max(...vals)
    const x = num(v)
    if (Number.isNaN(x) || hi === lo) return undefined
    const t = (x - lo) / (hi - lo)
    return `color-mix(in srgb, var(--color-accent-400) ${Math.round(8 + t * 55)}%, transparent)`
  }
  return (
    <div className="overflow-x-auto rounded-md border border-slate-800 bg-void-900/50">
      <table className="w-full border-collapse text-sm">
        {spec.columns.length > 0 && (
          <thead>
            <tr className="border-b border-slate-700/80">
              {cols.map((c, i) => (
                <th
                  key={i}
                  className={`px-3 py-1.5 font-mono text-[10px] font-semibold tracking-wider text-slate-400 uppercase ${
                    numeric[i] ? 'text-right' : 'text-left'
                  }`}
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {spec.rows.map((row, ri) => (
            <tr key={ri} className={ri % 2 === 1 ? 'bg-slate-800/20' : ''}>
              {cols.map((_, ci) => (
                <td
                  key={ci}
                  style={{ background: heatFor(ci, row[ci] ?? '') }}
                  className={`px-3 py-1.5 text-slate-200 ${
                    numeric[ci] ? 'text-right font-mono text-[13px] tabular-nums' : 'text-left'
                  }`}
                >
                  {row[ci] ?? ''}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
