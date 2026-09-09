import { render, screen } from '@testing-library/react'
import { Sparkline, filterOut, triggerOut } from '../pages/AmbientPage'

describe('M30 trigger builder serialization (spec §18.5)', () => {
  it('serializes each schedule kind', () => {
    const base = { seconds: '900', cron: '0 9 * * *', at: '2026-09-01T09:00:00Z', filters: [] }
    expect(triggerOut({ ...base, type: 'interval' })).toEqual({ type: 'interval', seconds: 900 })
    expect(triggerOut({ ...base, type: 'cron' })).toEqual({ type: 'cron', cron: '0 9 * * *' })
    expect(triggerOut({ ...base, type: 'once' })).toEqual({ type: 'once', at: '2026-09-01T09:00:00Z' })
  })

  it('serializes webhook triggers with §17.3 filter rows', () => {
    const out = triggerOut({
      type: 'webhook',
      seconds: '',
      cron: '',
      at: '',
      filters: [{ field: 'repo', op: 'equals', value: 'core' }],
    })
    expect(out).toEqual({
      type: 'webhook',
      filters: [{ field: 'repo', op: 'equals', value: 'core' }],
    })
  })

  it('one_of filters split the comma list into values', () => {
    expect(filterOut({ field: 'sev', op: 'one_of', value: 'high, critical' })).toEqual({
      field: 'sev',
      op: 'one_of',
      value: '',
      values: ['high', 'critical'],
    })
  })
})

describe('M30 precision sparkline', () => {
  it('renders one tick per judged item', () => {
    render(<Sparkline series={[1, 0, 1, 1]} />)
    const svg = screen.getByRole('img', { name: /judged series: 1011/ })
    expect(svg.querySelectorAll('rect')).toHaveLength(4)
  })

  it('renders a placeholder with no judged items', () => {
    render(<Sparkline series={[]} />)
    expect(screen.getByText('no judged items')).toBeInTheDocument()
  })
})
