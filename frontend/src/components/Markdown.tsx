// Safe markdown rendering for the canonical answer (spec §8.5): react-markdown
// renders to React elements — no raw HTML pass-through, no injection surface.
import ReactMarkdown from 'react-markdown'

const COMPONENTS: React.ComponentProps<typeof ReactMarkdown>['components'] = {
  h1: (p) => <h3 className="font-display mt-3 mb-1.5 text-base text-accent-300" {...p} />,
  h2: (p) => <h4 className="font-display mt-3 mb-1 text-sm text-accent-300" {...p} />,
  h3: (p) => <h5 className="font-display mt-2 mb-1 text-sm text-slate-100" {...p} />,
  p: (p) => <p className="my-1.5 leading-relaxed first:mt-0 last:mb-0" {...p} />,
  ul: (p) => <ul className="my-1.5 list-disc space-y-0.5 pl-5" {...p} />,
  ol: (p) => <ol className="my-1.5 list-decimal space-y-0.5 pl-5" {...p} />,
  li: (p) => <li className="leading-relaxed" {...p} />,
  strong: (p) => <strong className="font-semibold text-slate-100" {...p} />,
  em: (p) => <em {...p} />,
  a: (p) => (
    <a
      className="text-accent-400 underline decoration-accent-400/40 hover:decoration-accent-400"
      target="_blank"
      rel="noreferrer"
      {...p}
    />
  ),
  code: (p) => (
    <code
      className="rounded bg-slate-800/80 px-1 py-0.5 font-mono text-[0.85em] text-accent-300"
      {...p}
    />
  ),
  pre: (p) => (
    <pre
      className="my-2 overflow-x-auto rounded-md border border-slate-800 bg-void-950/60 p-2.5 text-xs"
      {...p}
    />
  ),
  blockquote: (p) => (
    <blockquote className="my-1.5 border-l-2 border-slate-700 pl-3 text-slate-400" {...p} />
  ),
  hr: () => <hr className="my-2 border-slate-800" />,
  table: (p) => <table className="my-2 w-full border-collapse text-xs" {...p} />,
  th: (p) => (
    <th className="border-b border-slate-700 px-2 py-1 text-left text-slate-100" {...p} />
  ),
  td: (p) => <td className="border-b border-slate-800/60 px-2 py-1" {...p} />,
}

export function Markdown({ text }: { text: string }) {
  return <ReactMarkdown components={COMPONENTS}>{text}</ReactMarkdown>
}
