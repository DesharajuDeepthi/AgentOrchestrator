import { render } from '@testing-library/react'
import { AnswerBlock } from '../components/AnswerPanel'
import { AnswerUiView } from '../components/AnswerUiView'

const VALID_A2UI = [
  {
    version: 'v0.9',
    createSurface: {
      surfaceId: 'answer',
      catalogId: 'https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json',
    },
  },
  {
    version: 'v0.9',
    updateComponents: {
      surfaceId: 'answer',
      components: [
        { id: 'c1', component: 'Text', text: 'Revenue was down 12%.' },
        { id: 'root', component: 'Column', children: ['c1'] },
      ],
    },
  },
]

describe('AnswerUiView (spec §8.5: official @a2ui/react renderer)', () => {
  it('renders A2UI v0.9 messages through the official processor', () => {
    const { container } = render(<AnswerUiView messages={VALID_A2UI} />)
    expect(container.textContent).toContain('Revenue was down 12%.')
  })

  it('is failure-safe: garbage payloads render nothing, never throw', () => {
    const { container } = render(<AnswerUiView messages={[{ not: 'a2ui' }]} />)
    expect(container.querySelector('.a2ui-answer')?.textContent ?? '').toBe('')
  })
})

const CHART = {
  kind: 'bar' as const,
  title: 'Mid-flow chart',
  labels: ['a', 'b'],
  series: [{ name: 's', values: [1, 2] }],
}
const a2uiSegment = (text: string, id: string) => [
  {
    version: 'v0.9',
    createSurface: {
      surfaceId: 'answer',
      catalogId: 'https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json',
    },
  },
  {
    version: 'v0.9',
    updateComponents: {
      surfaceId: 'answer',
      components: [
        { id, component: 'Text', text },
        { id: 'root', component: 'Column', children: [id] },
      ],
    },
  },
]

describe('AnswerBlock blocks rendering (spec §8.5: charts where they matter)', () => {
  it('renders ordered blocks: segment, chart, segment — chart between the texts', () => {
    const payload = {
      a2ui: a2uiSegment('ignored full tree', 'x1'),
      presentation: 'a2ui_first',
      coverage: 100,
      charts: [CHART],
      blocks: [
        { a2ui: a2uiSegment('Before the chart.', 's1') },
        { chart: CHART },
        { a2ui: a2uiSegment('After the chart.', 's2') },
      ],
    }
    const { container } = render(
      <AnswerBlock markdown="raw" payload={payload} toolCharts={[]} />,
    )
    const text = container.textContent ?? ''
    const before = text.indexOf('Before the chart.')
    const title = text.indexOf('Mid-flow chart')
    const after = text.indexOf('After the chart.')
    expect(before).toBeGreaterThanOrEqual(0)
    expect(title).toBeGreaterThan(before)
    expect(after).toBeGreaterThan(title)
  })

  it('tool_chart_ref places the tool chart inline and dedupes the bottom slot', () => {
    const payload = {
      a2ui: a2uiSegment('tree', 'x1'),
      presentation: 'a2ui_first',
      coverage: 100,
      blocks: [
        { a2ui: a2uiSegment('Discussed here:', 's1') },
        { tool_chart_ref: 0 },
      ],
    }
    const { container } = render(
      <AnswerBlock markdown="raw" payload={payload} toolCharts={[CHART]} />,
    )
    // exactly one rendering of the chart title: inline, not repeated below
    const matches = (container.textContent ?? '').split('Mid-flow chart').length - 1
    expect(matches).toBe(1)
  })

  it('table blocks render as native tables at their position', () => {
    const payload = {
      a2ui: a2uiSegment('tree', 'x1'),
      presentation: 'a2ui_first',
      coverage: 100,
      blocks: [
        { a2ui: a2uiSegment('Comparison below.', 's1') },
        { table: { columns: ['Line', 'Units'], rows: [['3', '120'], ['4', '80']] } },
        { a2ui: a2uiSegment('Line 4 trails.', 's2') },
      ],
    }
    const { container } = render(<AnswerBlock markdown="raw" payload={payload} toolCharts={[]} />)
    const table = container.querySelector('table')
    expect(table).not.toBeNull()
    expect(table?.querySelectorAll('th').length).toBe(2)
    expect(table?.querySelectorAll('tbody tr').length).toBe(2)
    const text = container.textContent ?? ''
    expect(text.indexOf('Comparison below.')).toBeLessThan(text.indexOf('120'))
    expect(text.indexOf('120')).toBeLessThan(text.indexOf('Line 4 trails.'))
  })

  it('legacy payloads without blocks keep the hoisted-top layout', () => {
    const payload = {
      a2ui: a2uiSegment('Legacy body.', 'x1'),
      presentation: 'a2ui_first',
      coverage: 100,
      charts: [CHART],
    }
    const { container } = render(<AnswerBlock markdown="raw" payload={payload} toolCharts={[]} />)
    const text = container.textContent ?? ''
    expect(text.indexOf('Mid-flow chart')).toBeLessThan(text.indexOf('Legacy body.'))
  })
})

import { ChartSvg } from '../components/ChartSvg'

describe('ChartSvg kinds (spec §7.1)', () => {
  const base = { title: 'T', labels: ['a', 'b', 'c'], series: [{ name: 's', values: [1, 2, 3] }] }
  const kinds = [
    'bar', 'hbar', 'stacked_bar', 'stacked_bar_100', 'line', 'area', 'stacked_area',
    'pie', 'donut', 'histogram', 'funnel', 'waterfall', 'lollipop',
  ] as const
  for (const kind of kinds) {
    it(`renders an svg for kind=${kind}`, () => {
      const { container } = render(<ChartSvg spec={{ ...base, kind }} />)
      expect(container.querySelector('svg')).not.toBeNull()
      expect(
        container.querySelectorAll('rect, polyline, path, polygon, circle').length,
      ).toBeGreaterThan(0)
    })
  }

  const special: [string, object][] = [
    ['gauge', { title: 'G', labels: ['CPU'], series: [{ name: '', values: [42, 100] }] }],
    ['sparkline', { title: '', labels: [], series: [{ name: '', values: [1, 3, 2, 5] }] }],
    ['scatter', { title: 'S', labels: [], series: [{ name: 'a', values: [], points: [[1, 2], [3, 4]] }] }],
    ['bubble', { title: 'B', labels: [], series: [{ name: 'a', values: [], points: [[1, 2, 5], [3, 4, 9]] }] }],
    ['candlestick', { title: 'C', labels: ['d1', 'd2'], series: [
      { name: 'open', values: [10, 12] }, { name: 'high', values: [14, 15] },
      { name: 'low', values: [9, 11] }, { name: 'close', values: [12, 11] }] }],
    ['boxplot', { title: 'X', labels: ['g1'], series: [
      { name: 'min', values: [1] }, { name: 'q1', values: [2] }, { name: 'median', values: [3] },
      { name: 'q3', values: [4] }, { name: 'max', values: [5] }] }],
    ['gantt', { title: 'P', labels: ['t1', 't2'], series: [],
      ranges: [['2026-08-01', '2026-08-04'], ['2026-08-03', '2026-08-09']] }],
    ['combo', { title: 'M', labels: ['a', 'b'], series: [
      { name: 'vol', values: [5, 7], render: 'bar' }, { name: 'px', values: [6, 8], render: 'line' }] }],
  ]
  for (const [kind, fixture] of special) {
    it(`renders an svg for kind=${kind} (shaped data)`, () => {
      const { container } = render(<ChartSvg spec={{ kind, ...fixture } as never} />)
      expect(container.querySelector('svg')).not.toBeNull()
      expect(
        container.querySelectorAll('rect, polyline, path, polygon, circle, line').length,
      ).toBeGreaterThan(0)
    })
  }

  it('heat tables color numeric cells per column', () => {
    const { container } = render(
      <AnswerBlock
        markdown="raw"
        payload={{
          a2ui: a2uiSegment('x', 'h1'),
          presentation: 'a2ui_first',
          coverage: 100,
          blocks: [
            { table: { columns: ['w', 'n'], rows: [['a', '1'], ['b', '9']], heat: true } },
          ],
        }}
        toolCharts={[]}
      />,
    )
    const cells = Array.from(container.querySelectorAll('td'))
    const shaded = cells.filter((c) => (c as HTMLElement).style.background)
    expect(shaded.length).toBe(2) // only the numeric column shades
  })

  it('thins dense date labels and drops the year after the first tick', () => {
    const labels = Array.from({ length: 30 }, (_, i) =>
      `2026-08-${String(i + 1).padStart(2, '0')}`,
    )
    const { container } = render(
      <ChartSvg
        spec={{ kind: 'line', title: '', labels, series: [{ name: '', values: labels.map((_, i) => i) }] }}
      />,
    )
    const texts = Array.from(container.querySelectorAll('svg text')).map((t) => t.textContent)
    const dateTicks = texts.filter((t) => t && /^\d{2}-\d{2}$|^\d{4}-/.test(t))
    expect(dateTicks.length).toBeLessThanOrEqual(9)
    expect(dateTicks[0]).toMatch(/^2026-/)
    expect(dateTicks.slice(1).every((t) => !t?.startsWith('2026-'))).toBe(true)
  })
})
