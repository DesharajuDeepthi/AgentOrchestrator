import type { ReactNode } from 'react'
import { useState } from 'react'
import type { Source, Status } from '../api/types'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

// ── badges (consistent table pattern, spec §8) ──────────────────

export function SourceBadge({ source }: { source: Source }) {
  return (
    <span
      className={cx(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
        source === 'static' ? 'bg-amber-500/15 text-amber-400' : 'bg-sky-500/15 text-sky-400',
      )}
    >
      {source}
    </span>
  )
}

export function KindBadge({ kind }: { kind: string }) {
  const tones: Record<string, string> = {
    mcp: 'bg-violet-500/15 text-violet-400',
    a2a: 'bg-cyan-500/15 text-cyan-400',
    native: 'bg-emerald-500/15 text-emerald-400',
    custom: 'bg-blue-500/15 text-blue-400',
    dynamic: 'bg-fuchsia-500/15 text-fuchsia-400',
  }
  return (
    <span
      className={cx(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
        tones[kind] ?? 'bg-slate-500/15 text-slate-400',
      )}
    >
      {kind}
    </span>
  )
}

export function StatusPill({ status, title }: { status: Status | string; title?: string }) {
  const tones: Record<string, string> = {
    active: 'bg-emerald-500/15 text-emerald-400 ring-emerald-500/30',
    inactive: 'bg-slate-500/15 text-slate-400 ring-slate-500/30',
    error: 'bg-rose-500/15 text-rose-400 ring-rose-500/30',
    running: 'bg-sky-500/15 text-sky-400 ring-sky-500/30 animate-pulse',
    paused_hitl: 'bg-amber-500/15 text-amber-400 ring-amber-500/30',
    completed: 'bg-emerald-500/15 text-emerald-400 ring-emerald-500/30',
    failed: 'bg-rose-500/15 text-rose-400 ring-rose-500/30',
    cancelled: 'bg-slate-500/15 text-slate-400 ring-slate-500/30',
  }
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1',
        tones[status] ?? tones.inactive,
      )}
    >
      <span className="size-1.5 rounded-full bg-current" />
      {status}
    </span>
  )
}

export function Chip({
  children,
  onClick,
  tone = 'default',
}: {
  children: ReactNode
  onClick?: () => void
  tone?: 'default' | 'direct' | 'muted'
}) {
  const tones = {
    default: 'bg-indigo-500/10 text-indigo-300 hover:bg-indigo-500/25 ring-indigo-500/30',
    direct: 'bg-teal-500/10 text-teal-300 ring-teal-500/30',
    muted: 'bg-slate-800 text-slate-500 ring-slate-700',
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className={cx(
        'inline-flex max-w-48 items-center truncate rounded-full px-2 py-0.5 text-[11px] ring-1 transition-colors',
        tones[tone],
        onClick ? 'cursor-pointer' : 'cursor-default',
      )}
    >
      {children}
    </button>
  )
}

// ── controls ────────────────────────────────────────────────────

export function Button({
  children,
  onClick,
  variant = 'secondary',
  disabled,
  type = 'button',
  title,
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  disabled?: boolean
  type?: 'button' | 'submit'
  title?: string
}) {
  const variants = {
    primary: 'bg-indigo-600 text-white hover:bg-indigo-500 disabled:bg-indigo-900',
    secondary:
      'bg-slate-800 text-slate-200 hover:bg-slate-700 ring-1 ring-slate-700 disabled:opacity-40',
    danger: 'bg-rose-600/80 text-white hover:bg-rose-600 disabled:opacity-40',
    ghost: 'text-slate-400 hover:text-slate-200 hover:bg-slate-800',
  }
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={cx(
        'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed',
        variants[variant],
      )}
    >
      {children}
    </button>
  )
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label?: string
}) {
  return (
    <label className={cx('inline-flex items-center gap-2', disabled && 'opacity-50')}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cx(
          'relative h-5 w-9 shrink-0 rounded-full border p-0 transition-colors',
          checked ? 'border-indigo-500/60 bg-indigo-600' : 'border-slate-600/60 bg-slate-700',
        )}
      >
        <span
          className={cx(
            // explicit left anchor keeps the knob inside the 36px track on
            // every browser (buttons carry default padding otherwise), and
            // the ring keeps a white knob visible on light theme surfaces
            'absolute left-0.5 top-0.5 size-4 rounded-full bg-white shadow-sm ring-1 ring-black/20 transition-transform',
            checked && 'translate-x-4',
          )}
        />
      </button>
      {label && <span className="text-xs text-slate-400">{label}</span>}
    </label>
  )
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string
  children: ReactNode
  hint?: string
}) {
  return (
    <div className="space-y-1">
      <label className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </label>
      {children}
      {hint && <p className="text-[11px] text-slate-500">{hint}</p>}
    </div>
  )
}

export const inputCls =
  'w-full rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-sm text-slate-200 placeholder:text-slate-600 focus:border-indigo-500 focus:outline-none'

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(inputCls, props.className)} />
}

export function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cx(inputCls, 'font-mono text-xs', props.className)} />
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cx(inputCls, props.className)} />
}

// ── surfaces ────────────────────────────────────────────────────

export function Drawer({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  children: ReactNode
  wide?: boolean
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className={cx(
          'absolute inset-y-0 right-0 flex flex-col overflow-y-auto border-l border-slate-800 bg-slate-950 shadow-2xl',
          wide ? 'w-[52rem] max-w-[95vw]' : 'w-[30rem] max-w-[95vw]',
        )}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-800 bg-slate-950/95 px-5 py-3 backdrop-blur">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200">
            ✕
          </button>
        </div>
        <div className="flex-1 space-y-4 px-5 py-4">{children}</div>
      </div>
    </div>
  )
}

export function StaticNotice() {
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300">
      Static record — definition fields are locked; status and exposure toggles stay live.
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300 whitespace-pre-wrap">
      {message}
    </div>
  )
}

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-md border border-slate-800 bg-slate-900/70 p-3 text-[11px] leading-relaxed text-slate-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

export function MaskedValue({ value }: { value: string }) {
  const [revealed, setRevealed] = useState(false)
  return (
    <button
      type="button"
      onClick={() => setRevealed((r) => !r)}
      title={revealed ? 'click to mask' : 'click to reveal'}
      className="font-mono text-[11px] text-slate-400 hover:text-slate-200"
    >
      {revealed ? value : '•'.repeat(Math.min(Math.max(value.length, 6), 14))}
    </button>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-800 px-6 py-10 text-center text-sm text-slate-500">
      {children}
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
}) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">{title}</h1>
        {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}

export function timeAgo(iso: string | null): string {
  if (!iso) return '—'
  const delta = (Date.now() - new Date(iso).getTime()) / 1000
  if (delta < 60) return `${Math.max(1, Math.floor(delta))}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return new Date(iso).toLocaleString()
}

export function duration(start: string | null, end: string | null): string {
  if (!start || !end) return '—'
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
