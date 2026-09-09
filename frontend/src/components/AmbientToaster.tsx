import { useEffect, useState } from 'react'
import { sseUrl } from '../api/client'
import { useSettings } from '../api/hooks'
import { cx } from './ui'

export interface AmbientStreamEvent {
  id: string
  mode: 'interrupt' | 'notify' | 'digest'
  tier: number
  urgency: number
  category: string
  title: string
  at: string
}

interface Toast extends AmbientStreamEvent {
  key: string
}

const TOAST_MS = 8000
const MAX_TOASTS = 4

/** Ambient delivery toast (spec §18.4): subscribes to the global ambient
 * SSE stream ONLY while the settings snapshot says ambient is on — with
 * ambient dark there is no stream, no subscription, no toast. Tier-0/1
 * events toast; digests stay in the inbox. */
export function AmbientToaster() {
  const { data: settings } = useSettings()
  const ambientOn = Boolean(settings?.ambient_enabled)
  const [toasts, setToasts] = useState<Toast[]>([])

  useEffect(() => {
    if (!ambientOn) return
    const source = new EventSource(sseUrl('/ambient/stream'))
    source.onmessage = (e: MessageEvent) => {
      let event: AmbientStreamEvent
      try {
        event = JSON.parse(e.data as string) as AmbientStreamEvent
      } catch {
        return
      }
      if (event.tier > 1) return // digest/silent ride the inbox, not a toast
      const key = `${event.id}:${event.at}`
      setToasts((current) => [...current, { ...event, key }].slice(-MAX_TOASTS))
      setTimeout(() => setToasts((current) => current.filter((t) => t.key !== key)), TOAST_MS)
    }
    return () => source.close()
  }, [ambientOn])

  if (toasts.length === 0) return null
  return (
    <div
      data-testid="ambient-toaster"
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2"
    >
      {toasts.map((toast) => (
        <div
          key={toast.key}
          className={cx(
            'pointer-events-auto rounded-lg border bg-void-900/95 p-3 shadow-xl backdrop-blur',
            toast.mode === 'interrupt' ? 'border-accent-400/60' : 'border-slate-700',
          )}
        >
          <div className="flex items-center justify-between gap-2">
            <span
              className={cx(
                'font-mono text-[9px] uppercase tracking-[0.2em]',
                toast.mode === 'interrupt' ? 'text-accent-300' : 'text-slate-400',
              )}
            >
              ambient {toast.mode} · {toast.category}
            </span>
            <button
              aria-label="dismiss"
              className="text-slate-500 hover:text-slate-200"
              onClick={() => setToasts((current) => current.filter((t) => t.key !== toast.key))}
            >
              ×
            </button>
          </div>
          <div className="mt-1 text-[13px] font-medium text-slate-100">{toast.title}</div>
        </div>
      ))}
    </div>
  )
}
