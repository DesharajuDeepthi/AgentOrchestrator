// Registry-cache refresh button + status line (spec §7.3/§8.7): an operator
// override on top of event invalidation — never a correctness requirement.
import { useCacheStatus, useRefreshCache } from '../api/hooks'
import { Button, timeAgo } from './ui'

export function CacheControls({ registry }: { registry: 'tools' | 'skills' | 'sub_agents' }) {
  const { data: status } = useCacheStatus()
  const refresh = useRefreshCache()
  const entry = status?.registries[registry]
  const line = !status
    ? '…'
    : status.mode === 'bypass'
      ? 'cache: bypass (direct db reads)'
      : entry?.records != null
        ? `cache: ${status.mode} · ${entry.records} records · gen ${entry.generation}` +
          (entry.loaded_at ? ` · loaded ${timeAgo(entry.loaded_at)}` : '')
        : `cache: ${status.mode} · not loaded`
  return (
    <div className="flex items-center gap-3">
      <span className="font-mono text-[10px] tracking-wider text-slate-500 uppercase">{line}</span>
      <Button
        variant="secondary"
        disabled={refresh.isPending}
        onClick={() => refresh.mutate(registry)}
      >
        {refresh.isPending ? 'Refreshing…' : '⟳ Refresh cache'}
      </Button>
    </div>
  )
}
