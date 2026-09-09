/** Declarative answer UI renderer (spec §8.5): official @a2ui/react renderer
 * over A2UI v0.9 protocol messages — payloads are data, never markup. */
import { useEffect, useMemo, useState } from 'react'
import { MessageProcessor } from '@a2ui/web_core/v0_9'
import { A2uiSurface, MarkdownContext, basicCatalog } from '@a2ui/react/v0_9'
import { renderMarkdown } from '@a2ui/markdown-it'

export function AnswerUiView({ messages, bare = false }: { messages: unknown[]; bare?: boolean }) {
  const processor = useMemo(() => {
    const p = new MessageProcessor([basicCatalog])
    try {
      p.processMessages(messages as Parameters<typeof p.processMessages>[0])
    } catch {
      return null // failure-safe: invalid payloads render nothing
    }
    return p
  }, [messages])

  const [surfaces, setSurfaces] = useState(() =>
    processor ? Array.from(processor.model.surfacesMap.values()) : [],
  )
  useEffect(() => {
    if (!processor) return
    setSurfaces(Array.from(processor.model.surfacesMap.values()))
    const sync = () => setSurfaces(Array.from(processor.model.surfacesMap.values()))
    const created = processor.onSurfaceCreated(sync)
    const deleted = processor.onSurfaceDeleted(sync)
    return () => {
      created.unsubscribe()
      deleted.unsubscribe()
    }
  }, [processor])

  if (!processor || surfaces.length === 0) return null
  const rendered = (
    <MarkdownContext.Provider value={renderMarkdown}>
      {surfaces.map((surface) => (
        <A2uiSurface key={surface.id} surface={surface} />
      ))}
    </MarkdownContext.Provider>
  )
  // bare: a segment inside a blocks shell — the shell owns chrome + caption
  if (bare) return <div className="a2ui-answer">{rendered}</div>
  return (
    <div className="a2ui-answer mt-2 rounded-lg border border-accent-500/20 bg-slate-900/40 p-3">
      {/* labeled as a companion view so it never reads as a second answer */}
      <div className="mb-2 font-mono text-[9px] uppercase tracking-[0.2em] text-slate-500">
        answer panel · structured view
      </div>
      {rendered}
    </div>
  )
}
