import type { OverlapCheck } from '../api/types'
import { Button } from './ui'

/** Pre-save overlap guard dialog (spec §4): the LLM judge flagged the draft
 * as ≥threshold% overlapping an existing record — the user decides. */
export function OverlapDialog({
  check,
  entity,
  onConfirm,
  onCancel,
}: {
  check: OverlapCheck
  entity: 'skill' | 'sub agent'
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-void-950/80 backdrop-blur-sm">
      <div className="mx-4 w-full max-w-lg rounded-lg border border-amber-500/40 bg-void-900 p-6 shadow-2xl">
        <div className="mb-1 font-display text-sm uppercase tracking-widest text-amber-400">
          ⚠ possible duplicate — {check.overlap_percent}% overlap
        </div>
        <p className="mb-3 text-sm text-slate-300">
          This {entity} looks like it substantially overlaps the existing{' '}
          <span className="font-semibold text-slate-100">
            {check.match_type.replace('_', ' ')} “{check.match_name ?? 'unknown'}”
          </span>{' '}
          (judged at {check.overlap_percent}%, threshold {check.threshold}%).
        </p>
        <p className="mb-5 rounded-md border border-slate-800 bg-void-950/60 p-3 text-xs leading-relaxed text-slate-400">
          {check.reasoning}
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel}>
            Cancel — use “{check.match_name ?? 'the existing one'}” instead
          </Button>
          <Button variant="danger" onClick={onConfirm}>
            Save anyway
          </Button>
        </div>
      </div>
    </div>
  )
}
