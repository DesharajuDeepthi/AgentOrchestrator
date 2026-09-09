import { render, screen } from '@testing-library/react'
import { KindBadge, SourceBadge, StatusPill, timeAgo, duration } from '../components/ui'

describe('badges (consistent table pattern, spec §8)', () => {
  it('renders source badges for both write paths', () => {
    render(
      <>
        <SourceBadge source="static" />
        <SourceBadge source="dynamic" />
      </>,
    )
    expect(screen.getByText('static')).toBeInTheDocument()
    expect(screen.getByText('dynamic')).toBeInTheDocument()
  })

  it('renders kind badges for every tier discriminator', () => {
    render(
      <>
        <KindBadge kind="mcp" />
        <KindBadge kind="native" />
        <KindBadge kind="custom" />
        <KindBadge kind="dynamic" />
      </>,
    )
    for (const kind of ['mcp', 'native', 'custom', 'dynamic']) {
      expect(screen.getByText(kind)).toBeInTheDocument()
    }
  })

  it('renders run status pills incl. paused_hitl', () => {
    render(<StatusPill status="paused_hitl" />)
    expect(screen.getByText('paused_hitl')).toBeInTheDocument()
  })
})

describe('time helpers', () => {
  it('formats durations', () => {
    expect(duration('2026-01-01T00:00:00Z', '2026-01-01T00:00:02Z')).toBe('2.0s')
    expect(duration(null, null)).toBe('—')
  })
  it('formats relative time', () => {
    expect(timeAgo(null)).toBe('—')
    expect(timeAgo(new Date(Date.now() - 5000).toISOString())).toMatch(/s ago/)
  })
})
