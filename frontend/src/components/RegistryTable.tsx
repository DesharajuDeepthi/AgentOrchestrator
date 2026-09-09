/** Consistent table pattern (spec §8): search, source filter, kind filter,
 * badges, status pill, row click → drawer. */
import type { ReactNode } from 'react'
import { useState } from 'react'
import { EmptyState, Select, TextInput } from './ui'

export interface Column<T> {
  header: string
  render: (row: T) => ReactNode
  width?: string
}

export function RegistryTable<T extends { id: string }>({
  rows,
  columns,
  onRowClick,
  kinds,
  filterRow,
  loading,
  empty = 'Nothing here yet.',
}: {
  rows: T[]
  columns: Column<T>[]
  onRowClick?: (row: T) => void
  kinds?: string[]
  filterRow: (row: T, q: string, source: string, kind: string) => boolean
  loading?: boolean
  empty?: string
}) {
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [kind, setKind] = useState('')
  const visible = rows.filter((r) => filterRow(r, q.toLowerCase(), source, kind))
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <TextInput
          placeholder="Search…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="max-w-56"
        />
        <Select value={source} onChange={(e) => setSource(e.target.value)} className="max-w-36">
          <option value="">all sources</option>
          <option value="static">static</option>
          <option value="dynamic">dynamic</option>
        </Select>
        {kinds && (
          <Select value={kind} onChange={(e) => setKind(e.target.value)} className="max-w-36">
            <option value="">all kinds</option>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </Select>
        )}
        <span className="ml-auto text-[11px] text-slate-600">
          {visible.length} of {rows.length}
        </span>
      </div>
      {loading ? (
        <EmptyState>Loading…</EmptyState>
      ) : visible.length === 0 ? (
        <EmptyState>{empty}</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-900/80 text-[11px] uppercase tracking-wider text-slate-500">
              <tr>
                {columns.map((c) => (
                  <th key={c.header} className="px-3 py-2 font-semibold" style={{ width: c.width }}>
                    {c.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70">
              {visible.map((row) => (
                <tr
                  key={row.id}
                  onClick={() => onRowClick?.(row)}
                  className={
                    onRowClick
                      ? 'cursor-pointer transition-colors hover:bg-slate-900/60'
                      : undefined
                  }
                >
                  {columns.map((c) => (
                    <td key={c.header} className="px-3 py-2 align-middle">
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
